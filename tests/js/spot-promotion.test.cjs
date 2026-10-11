// tests/js/spot-promotion.test.cjs
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

function source() {
  return fs.readFileSync(
    path.join(__dirname, '../../static/js/scenarios.js'), 'utf8');
}

// buildTradePayload is a pure function; extract and evaluate just it, so the
// test does not need the whole 8000-line module's dependencies.
function loadBuilder() {
  const src = source();
  const start = src.indexOf('function buildSpotTradePayload');
  assert.ok(start !== -1, 'buildSpotTradePayload must exist');
  const end = src.indexOf('\n}', start) + 2;
  const sandbox = {};
  const vm = require('vm');
  vm.createContext(sandbox);
  vm.runInContext(src.slice(start, end) +
    '\nthis.fn = buildSpotTradePayload;', sandbox);
  return sandbox.fn;
}

test('a spot buy maps onto a long journal trade tagged spot', () => {
  const build = loadBuilder();
  const payload = build('BTC', 50000, 0.5, 12345);
  assert.strictEqual(payload.asset, 'BTC');
  assert.strictEqual(payload.direction, 'long');
  assert.strictEqual(payload.entry_price, 50000);
  assert.strictEqual(payload.size, 0.5);
  assert.strictEqual(payload.account_tag, 'spot');
  assert.strictEqual(payload.drawing_id, 12345);
});

test('payload carries no key the API model forbids', () => {
  // TradeCreateRequest sets extra="forbid" — an unknown key is a 422.
  const build = loadBuilder();
  const allowed = new Set(['asset', 'entry_price', 'direction', 'timeframe',
    'target_price', 'stop_price', 'indicators_at_entry', 'drawing_id',
    'scenario_id', 'size', 'fees', 'confidence', 'mood', 'account_tag',
    'tags', 'checklist_results']);
  Object.keys(build('BTC', 50000, 0.5, 1)).forEach(function(key) {
    assert.ok(allowed.has(key), 'unexpected key: ' + key);
  });
});

test('a buy with no quantity is not promoted', () => {
  const build = loadBuilder();
  assert.strictEqual(build('BTC', 50000, 0, 1), null);
  assert.strictEqual(build('BTC', 50000, null, 1), null);
});

test('a buy with no price is not promoted', () => {
  const build = loadBuilder();
  assert.strictEqual(build('BTC', 0, 0.5, 1), null);
});

// The sell path must close the trade the buy was promoted into. A sell
// must never close an unrelated entry, or the promoted trade would stay open
// and be double-counted in the book.
function loadSelector() {
  const src = source();
  const start = src.indexOf('function selectPromotedTradeId');
  assert.ok(start !== -1, 'selectPromotedTradeId must exist');
  const end = src.indexOf('\n}', start) + 2;
  const sandbox = {};
  const vm = require('vm');
  vm.createContext(sandbox);
  vm.runInContext(src.slice(start, end) +
    '\nthis.fn = selectPromotedTradeId;', sandbox);
  return sandbox.fn;
}

test('sell resolves the journal trade promoted from that drawing', () => {
  const select = loadSelector();
  const positions = [
    { id: 10, drawing_id: 1 },
    { id: 11, drawing_id: 2 },
  ];
  assert.strictEqual(select(positions, 2), 11);
});

test('sell resolves to null when the drawing was never promoted', () => {
  const select = loadSelector();
  assert.strictEqual(select([{ id: 10, drawing_id: 1 }], 99), null);
  assert.strictEqual(select([], 1), null);
  assert.strictEqual(select(null, 1), null);
});

test('a drawing id of zero does not match an unpromoted position', () => {
  // Pins the intended behaviour: an unpromoted drawing (drawing_id null)
  // must not match a falsy-but-valid drawingId of 0. Note this does NOT by
  // itself distinguish === from == — null == 0 is false under either
  // operator. See the next test for the case that actually discriminates.
  const select = loadSelector();
  assert.strictEqual(select([{ id: 10, drawing_id: null }], 0), null);
});

test('drawing_id resolves when the server returns it as a string (production Postgres shape)', () => {
  // tests/schema/carrac_pg_schema.sql — dumped from a deployed database — declares
  // `drawing_id text` on trade_journal (line 1801). psycopg2 therefore
  // returns this column as a Python str, and /api/positions/live JSON-
  // encodes it as a STRING, e.g. "drawing_id": "1754332800000". The
  // browser's own drawingId is always a number (Date.now()). A prior
  // version of this test asserted the string form must NOT match, calling
  // it "serialization drift" — it is not drift, it is the real production
  // type, on every request. Comparing loosely (==) would coerce correctly
  // here but would ALSO wrongly match unrelated values in other cases, so
  // the fix compares both sides as strings explicitly instead of relying
  // on `==` coercion — see the explicit String()/String() call in
  // selectPromotedTradeId.
  const select = loadSelector();
  assert.strictEqual(select([{ id: 10, drawing_id: '12345' }], 12345), 10);
  assert.strictEqual(select([{ id: 10, drawing_id: '0' }], 0), 10);
});

test('a null drawing_id still does not match a drawingId of 0, even under string comparison', () => {
  // The case the explicit null/undefined guard exists for: String(null) is
  // 'null' and String(0) is '0' — those are unequal anyway, but the guard
  // makes the "never match a real id" intent explicit rather than relying
  // on that string spelling never colliding with a real drawingId.
  const select = loadSelector();
  assert.strictEqual(select([{ id: 10, drawing_id: null }], 0), null);
  assert.strictEqual(select([{ id: 10, drawing_id: undefined }], 0), null);
});

// --- Wiring tests -----------------------------------------------------
//
// buildSpotTradePayload/selectPromotedTradeId being correct is worthless if
// nothing at the real hook sites calls them — "built but never connected"
// is this codebase's most common defect class (a hard-delete path doing
// exactly that was caught in Task 10 review). These assert against the
// actual call sites in kline-drawings.js / position-tools.js /
// chart-keyboard.js / scenarios.js, scoped
// to the specific function/block via anchor pairs — not "does this file
// contain the string anywhere" — so a real disconnection fails loud.

function readFile(relPath) {
  return fs.readFileSync(path.join(__dirname, '../..', relPath), 'utf8');
}

// Slice between two unique anchors so assertions are scoped to the real
// hook site rather than the whole file.
function between(src, startAnchor, endAnchor) {
  const s = src.indexOf(startAnchor);
  assert.ok(s !== -1, 'start anchor not found: ' + startAnchor);
  const e = src.indexOf(endAnchor, s + startAnchor.length);
  assert.ok(e !== -1, 'end anchor not found: ' + endAnchor);
  return src.slice(s, e);
}

test('wiring: spot-buy creation promotes with a drawing id threaded through extendData', () => {
  const src = readFile('static/js/kline-drawings.js');
  const block = between(src, "if (tool === 'spot') {", "if (tool === 'spotSell') {");
  assert.match(block, /_journalDrawingId\s*:\s*journalDrawingId/,
    'buy creation must stamp _journalDrawingId onto extendData so the sell/delete paths can find it later');
  assert.match(block, /_promoteSpotToJournal\(\s*klineChartSymbol\s*,\s*price\s*,\s*qty\s*,\s*journalDrawingId\s*\)/,
    'buy creation must call _promoteSpotToJournal(symbol, price, qty, journalDrawingId)');
});

test('wiring: sell path resolves and closes the promoted trade, not a fresh one', () => {
  // closeSpotWithJournal is the last function in position-tools.js, so the
  // block runs from its signature to the end of that file.
  const src = readFile('static/js/position-tools.js');
  const start = src.indexOf('function closeSpotWithJournal(overlayId, spotData, sellData, onComplete) {');
  assert.ok(start !== -1, 'closeSpotWithJournal not found in position-tools.js');
  assert.strictEqual(src.indexOf('\nfunction ', start + 1), -1,
    'closeSpotWithJournal is no longer the last function in position-tools.js — scope this block with an end anchor');
  const block = src.slice(start);
  assert.match(block, /_findPromotedTradeId\(\s*ext\._journalDrawingId\s*,/,
    'sell path must look up the promoted trade by ext._journalDrawingId');
  assert.match(block, /journalClose\(\s*promotedId\s*\)/,
    'sell path must close the resolved promoted trade id');
});

test('wiring: right-click / keyboard Delete / Drawings-tab / multi-select delete cleans up a promoted spot trade', () => {
  // handleContextMenuAction('delete', ...) is the one implementation shared
  // by all four of these entry points (per its own comment).
  const src = readFile('static/js/kline-drawings.js');
  const block = between(src, "if (action === 'delete') {", "} else if (action === 'edit-color') {");
  assert.match(block, /_deletePromotedSpotTrade\(\s*klineChartSymbol\s*,\s*overlayId\s*,/,
    "handleContextMenuAction's delete branch must call _deletePromotedSpotTrade");
});

test('wiring: the drawer\'s hard-delete popup button cleans up a promoted spot trade', () => {
  const src = readFile('static/js/scenarios.js');
  const block = between(src,
    "document.getElementById('spot-delete-hard').addEventListener('click', function() {",
    "document.getElementById('spot-mark-sold').addEventListener('click', function() {");
  assert.match(block, /_deletePromotedSpotTrade\(\s*symbol\s*,\s*drawingId\s*,/,
    "deleteSpot's Delete button must call _deletePromotedSpotTrade");
});

test('wiring: "clear all drawings" (both Shift+Delete and its Shift+D alias) clean up promoted spot trades first', () => {
  const src = readFile('static/js/chart-keyboard.js');
  const shiftD = between(src,
    '// Shift+D - clear ALL drawings (alias for Shift+Delete)',
    '// D key (no shift)');
  const shiftDelete = between(src,
    '// Shift+Delete - clear ALL drawings for current asset (with confirmation)',
    '// Delete/Backspace - remove the selected drawing');
  assert.match(shiftD, /_cleanupPromotedSpotTradesForSymbol\(\s*klineChartSymbol\s*\)/,
    'Shift+D clear-all must clean up promoted spot trades before wiping storage');
  assert.match(shiftDelete, /_cleanupPromotedSpotTradesForSymbol\(\s*klineChartSymbol\s*\)/,
    'Shift+Delete clear-all must clean up promoted spot trades before wiping storage');
});

test('wiring: the toolbar "Clear" button cleans up promoted spot trades first', () => {
  // Functionally identical to the Shift+Delete / Shift+D handlers (wipes
  // every drawing for the current symbol) but reaches storage through a
  // different path (_removeDrawingOverlays + clearKlineDrawingsFromStorage,
  // not handleContextMenuAction) — the most discoverable delete-everything
  // control in the UI (a labelled red "Clear" toolbar button), so it needed
  // its own call, found missing in Task 10 review round 2.
  const src = readFile('static/js/kline-drawings.js');
  const block = between(src, "if (tool === 'clear') {", "if (tool === 'magnet') {");
  assert.match(block, /_cleanupPromotedSpotTradesForSymbol\(\s*klineChartSymbol\s*\)/,
    'the Clear toolbar button must clean up promoted spot trades before wiping storage');
});

test('wiring: a promoted-trade delete hits the trade DELETE endpoint, not /close, and logs failure loudly', () => {
  // Deliberate design choice (see task-10-report.md): a hard delete means
  // "this was never a real position" — DELETE the trade. Closing it would
  // fabricate an exit price/P&L for a sale that never happened.
  const src = source();
  const start = src.indexOf('function _deletePromotedSpotTrade');
  assert.ok(start !== -1, '_deletePromotedSpotTrade must exist');
  const end = src.indexOf('\nfunction _cleanupPromotedSpotTradesForSymbol', start);
  assert.ok(end !== -1, '_cleanupPromotedSpotTradesForSymbol must exist right after it');
  const body = src.slice(start, end);
  assert.match(body, /method:\s*'DELETE'/, 'must hard-delete the journal trade');
  assert.doesNotMatch(body, /\/close/, 'must not hit the /close endpoint — that would fabricate an exit');
  assert.match(body, /carracLog/, 'a failed cleanup must be logged loudly, not swallowed');
});
