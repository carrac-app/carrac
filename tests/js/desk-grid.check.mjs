// tests/js/desk-grid.check.mjs — run: node tests/js/desk-grid.check.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { COLUMNS, DEFAULT_KEYS, buildSections } = require('../../static/js/desk-grid.js');
const asset = {
  symbol: 'BTC', latest_price: 118000.5, realtime: { change_pct: 1.23 },
  ema_ribbon: { trend: 'bull', days_since_flip: 12 },
  structure: { last_high_type: 'HH', last_low_type: 'HL', structure_trend: 'bullish' },
  bb: { squeeze_pct: 83.2 }, cpr: { price_level: 'CP-TC' }, data_stale: false,
  weekly_rsi: 55,
};
const by = Object.fromEntries(COLUMNS.map(c => [c.key, c]));
const eq = (k, want) => {
  const got = by[k].format(asset, { activityBySymbol: { BTC: 3 } });
  if (got !== want) { console.error(`${k}: got "${got}" want "${want}"`); process.exit(1); }
};
const eqSortVal = (k, want, testAsset) => {
  const got = by[k].sortVal(testAsset || asset, { activityBySymbol: { BTC: 3 } });
  if (got !== want) { console.error(`${k} sortVal: got "${got}" want "${want}"`); process.exit(1); }
};
const contains = (k, substr, testAsset) => {
  const got = by[k].format(testAsset || asset, { activityBySymbol: { BTC: 3 } });
  if (!got.includes(substr)) { console.error(`${k}: got "${got}" does not contain "${substr}"`); process.exit(1); }
};
const notContains = (k, substr) => {
  const got = by[k].format(asset, { activityBySymbol: { BTC: 3 } });
  if (got.includes(substr)) { console.error(`${k}: got "${got}" should not contain "${substr}"`); process.exit(1); }
};

// ── Untouched-by-Batch-A columns — still render byte-identically ─────────
contains('symbol', '>BTC</a>');
notContains('symbol', 'stale-chip');
eq('price', '118000.5');
eq('squeeze', '83%');

// Stale variant
const staleAsset = { ...asset, data_stale: true };
const staleBtc = by['symbol'].format(staleAsset, { activityBySymbol: { BTC: 3 } });
if (!staleBtc.includes('stale-chip')) { console.error(`symbol stale: got "${staleBtc}" does not contain "stale-chip"`); process.exit(1); }

// sortVal tests (unchanged by Batch A — only markup/cellClass changed for the 4 columns)
eqSortVal('structure', 'bullish');
eqSortVal('changed', 12);
eqSortVal('changed', 1e9, { ...asset, ema_ribbon: {} });
eqSortVal('price', 118000.5);

// ── Batch A: chg/ribbon/changed/structure now mirror main.js createRow()'s
// own markup/classes (guarded window.* helpers under a real browser; the
// assertions below exercise the Node-safe fallback path, since this script
// has no `window`/DOM — see each column's format() in desk-grid.js). ───────

// chg — signed, colored <span>, no window._fmtN under Node so falls back to
// toFixed(2). Positive AND negative branches both exercised.
contains('chg', '<span class="price-change price-change-up">+1.23%</span>');
contains('chg', 'price-change-down', { ...asset, realtime: { change_pct: -2.5 } });
{
  const noChg = by['chg'].format({ ...asset, realtime: {} }, {});
  if (noChg !== '--') { console.error(`chg no-data: got "${noChg}" want "--"`); process.exit(1); }
}
if (by['chg'].cellClass(asset) !== 'change-col') { console.error('chg cellClass mismatch'); process.exit(1); }

// ribbon — Node fallback still returns the plain label (no window.formatEmaRibbon
// under Node); cellClass carries prod's real <td> class pair.
eq('ribbon', 'Above');
if (by['ribbon'].cellClass(asset) !== 'ema-ribbon ema-ribbon-col') { console.error('ribbon cellClass mismatch'); process.exit(1); }

// changed — now a titled <span>, "<N>d" preferred over the bare date, plus
// the fresh-flip gradient/tooltip branch when trend_analysis.patterns has
// 'fresh_flip'. Deliberately reproduces prod's literal "999d" (no sentinel
// special-case) for "never flipped" — see batch-a-report.md.
contains('changed', '12d');
contains('changed', 'title="EMA Ribbon last flipped');
contains('changed', '999d', { ...asset, ema_ribbon: { trend: 'bull', days_since_flip: 999 } });
contains('changed', 'highlight-flip-to-bull', { ...asset, trend_analysis: { patterns: ['fresh_flip'] } });
contains('changed', 'data-tooltip="Fresh EMA Ribbon Flip"', { ...asset, trend_analysis: { patterns: ['fresh_flip'] } });
contains('changed', 'highlight-flip-to-bear', { ...asset, ema_ribbon: { trend: 'bear', days_since_flip: 3 }, trend_analysis: { patterns: ['fresh_flip'] } });
if (by['changed'].cellClass(asset) !== 'changed-col ema-ribbon-date') { console.error('changed cellClass mismatch'); process.exit(1); }

// structure — Node fallback unchanged; cellClass carries prod's real pair.
eq('structure', 'HH·HL');
if (by['structure'].cellClass(asset) !== 'structure-col structure-cell') { console.error('structure cellClass mismatch'); process.exit(1); }

// ── Slice A: column-picker registry expansion ────────────────────────────

// Registry now covers the full legacy screener column set (Core + Indicators
// + Extended), well beyond the original focused 9.
if (COLUMNS.length < 30) {
  console.error(`expected >= 30 columns (full legacy set), got ${COLUMNS.length}`);
  process.exit(1);
}

// Default-enabled set is exactly the original 9 — unchanged order/identity.
const ORIGINAL_9 = ['symbol', 'price', 'chg', 'ribbon', 'changed', 'structure', 'squeeze', 'cpr', 'activity'];
if (JSON.stringify(DEFAULT_KEYS) !== JSON.stringify(ORIGINAL_9)) {
  console.error(`DEFAULT_KEYS changed: got ${JSON.stringify(DEFAULT_KEYS)} want ${JSON.stringify(ORIGINAL_9)}`);
  process.exit(1);
}
// Every default key must resolve to a real registry entry.
for (const k of DEFAULT_KEYS) {
  if (!by[k]) { console.error(`DEFAULT_KEYS references unknown column "${k}"`); process.exit(1); }
}

// Every column's format() must run under Node (no `window`/DOM) without
// throwing on the existing fixture, and always return a string ('--' for
// window.format*-gated columns, since `window` is undefined here).
const opts = { activityBySymbol: { BTC: 3 } };
for (const c of COLUMNS) {
  let got;
  try {
    got = c.format(asset, opts);
  } catch (e) {
    console.error(`${c.key}: format() threw under Node: ${e.message}`);
    process.exit(1);
  }
  if (typeof got !== 'string') {
    console.error(`${c.key}: format() returned non-string (${typeof got})`);
    process.exit(1);
  }
  if (c.cellClass) {
    let cc;
    try {
      cc = c.cellClass(asset, opts);
    } catch (e) {
      console.error(`${c.key}: cellClass() threw under Node: ${e.message}`);
      process.exit(1);
    }
    if (cc !== undefined && cc !== null && typeof cc !== 'string') {
      console.error(`${c.key}: cellClass() returned non-string (${typeof cc})`);
      process.exit(1);
    }
  }
  let sortGot;
  try {
    sortGot = c.sortVal(asset, opts);
  } catch (e) {
    console.error(`${c.key}: sortVal() threw under Node: ${e.message}`);
    process.exit(1);
  }
  if (sortGot === undefined) {
    console.error(`${c.key}: sortVal() returned undefined`);
    process.exit(1);
  }
}

// One legacy column's sortVal sanity check — W-RSI is numeric off the fixture.
eqSortVal('rsi', 55);

// ── Batch A: buildSections() — the testable core of watchlist-first
// sectioning (task 2). Pure/Node-safe: no DOM, exported directly.
{
  const assets = [
    { symbol: 'BTC', name: 'Bitcoin', type: 'crypto' },
    { symbol: 'ETH', name: 'Ethereum', type: 'crypto' },
    { symbol: 'AAPL', name: 'Apple', type: 'stock' },
    { symbol: 'SPX', name: 'S&P 500', type: 'index' },
  ];

  // Watchlist first, in the caller's watchlist order (not re-sorted/deduped
  // out of its type section) — mirrors main.js's 'All'-tab pinned block.
  let sections = buildSections(assets, { watchlistSymbols: ['ETH', 'BTC'] });
  if (sections[0].key !== 'watchlist' || sections[0].count !== 2) {
    console.error(`buildSections: expected watchlist section first with count 2, got ${JSON.stringify(sections[0])}`);
    process.exit(1);
  }
  if (sections[0].rows.map(r => r.symbol).join(',') !== 'ETH,BTC') {
    console.error(`buildSections: watchlist section order wrong: ${sections[0].rows.map(r => r.symbol)}`);
    process.exit(1);
  }
  const cryptoSec = sections.find(s => s.key === 'crypto');
  if (!cryptoSec || cryptoSec.count !== 2) {
    console.error('buildSections: crypto section should still contain both BTC and ETH (prod pins, does not dedupe)');
    process.exit(1);
  }
  // Section order matches prod's sectionOrder (score-formatting.js:122):
  // crypto_metric, crypto, index, forex, stock, etf, commodity.
  const typeKeys = sections.filter(s => s.key !== 'watchlist').map(s => s.key);
  if (JSON.stringify(typeKeys) !== JSON.stringify(['crypto', 'index', 'stock'])) {
    console.error(`buildSections: type section order wrong: ${JSON.stringify(typeKeys)}`);
    process.exit(1);
  }

  // No watchlist symbols -> no watchlist section at all.
  sections = buildSections(assets, { watchlistSymbols: [] });
  if (sections.some(s => s.key === 'watchlist')) {
    console.error('buildSections: watchlist section should be absent when nothing is starred');
    process.exit(1);
  }

  // Quick filter narrows within sections and hides sections left empty.
  sections = buildSections(assets, { watchlistSymbols: ['BTC'], quickFilter: 'AAPL' });
  if (sections.length !== 1 || sections[0].key !== 'stock' || sections[0].count !== 1) {
    console.error(`buildSections: quick filter should leave only the stock section, got ${JSON.stringify(sections)}`);
    process.exit(1);
  }
  // Filter also strips a non-matching watchlist row (BTC doesn't match 'AAPL').
  if (sections.some(s => s.key === 'watchlist')) {
    console.error('buildSections: watchlist section should be filtered out when its only row does not match');
    process.exit(1);
  }

  // Name substring match (not just symbol).
  sections = buildSections(assets, { watchlistSymbols: [], quickFilter: 'ETHEREUM' });
  if (sections.length !== 1 || sections[0].rows[0].symbol !== 'ETH') {
    console.error('buildSections: quick filter should match on name too');
    process.exit(1);
  }
}

console.log(`desk-grid check OK (${COLUMNS.length} columns, ${DEFAULT_KEYS.length} default-on)`);
