// tests/js/desk-stream.check.mjs — run: node tests/js/desk-stream.check.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { flatten, groupBySymbol, matchesTab } = require('../../static/js/desk-stream.js');

// One raw /api/stream-shaped entry for BTC. Field names match
// carrac/scanner/routes.py:139-472 (entry/zone/liquidation shape) and
// carrac/indicator_state.py detect_transitions() (transition shape).
const data = {
  entries: [
    {
      symbol: 'BTC',
      price: 100,
      zones: [
        // Near zone: |102-100|/100 = 0.02 <= 0.05 -> kept.
        { price: 102, label: 'near zone', side: 'resistance' },
        // Far zone: |140-100|/100 = 0.4 > 0.05 -> dropped by the proximity gate.
        { price: 140, label: 'far zone', side: 'resistance' },
      ],
      signals: [
        { type: 'engulfing', direction: 'bullish' },
      ],
      transitions: [
        { indicator: 'ema_ribbon_flip', label: 'EMA Ribbon flip', from: 'bear', to: 'bull',
          significance: 2, symbol: 'BTC', ts: '2026-07-19T10:00:00Z' },
      ],
      liquidation: { m15_long_usd: 100000, m15_short_usd: 500000, dominant: 'short', total_usd: 600000 },
    },
  ],
  positions: [],
  stream_event: null,
};

const rows = flatten(data);

const fail = (msg) => { console.error(msg); process.exit(1); };

// Zone proximity gate: exactly one zone row (the near one; far dropped).
const zoneRows = rows.filter((r) => r.row_type === 'zone');
if (zoneRows.length !== 1) fail(`expected 1 zone row, got ${zoneRows.length}`);
if (zoneRows[0].label !== 'near zone') fail(`expected the near zone to survive, got "${zoneRows[0].label}"`);

// Signal row: bullish engulfing -> label contains 'Bull'.
const signalRows = rows.filter((r) => r.row_type === 'signal');
if (signalRows.length !== 1) fail(`expected 1 signal row, got ${signalRows.length}`);
if (!signalRows[0].label.includes('Bull')) fail(`signal label "${signalRows[0].label}" does not contain "Bull"`);

// Transition row exists.
const transitionRows = rows.filter((r) => r.row_type === 'transition');
if (transitionRows.length !== 1) fail(`expected 1 transition row, got ${transitionRows.length}`);
if (!transitionRows[0].label.includes('EMA Ribbon flip')) {
  fail(`transition label "${transitionRows[0].label}" does not contain "EMA Ribbon flip"`);
}

// Liquidation row picks the dominant side (short, per the fixture).
const liqRows = rows.filter((r) => r.row_type === 'liquidation');
if (liqRows.length !== 1) fail(`expected 1 liquidation row, got ${liqRows.length}`);
if (!liqRows[0].label.includes('short')) fail(`liquidation label "${liqRows[0].label}" does not mention "short"`);
if (!liqRows[0].label.includes('$500K')) fail(`liquidation label "${liqRows[0].label}" does not mention "$500K"`);

// activityBySymbol() reads module-private state (_entries) that only
// refresh() sets, and refresh() does a real fetch — so instead of faking a
// fetch, call flatten() directly and count rows per symbol the same way
// activityBySymbol() does. Confirms the far zone's exclusion (verified
// above) also means it's never counted toward BTC's activity total: 1
// zone + 1 signal + 1 transition + 1 liquidation = 4, not 5.
const activityBySymbol = {};
rows.forEach((r) => { if (r.symbol) activityBySymbol[r.symbol] = (activityBySymbol[r.symbol] || 0) + 1; });
if (activityBySymbol.BTC !== 4) fail(`expected activityBySymbol.BTC === 4 (far zone excluded), got ${activityBySymbol.BTC}`);

// --- groupBySymbol (slice B, by-asset view) -------------------------------
// Rows deliberately NOT type-priority-ordered within each symbol, to prove
// topTag picks by TYPE_PRIORITY (transition > signal > liquidation > zone >
// event > position — see desk-stream.js comment above TYPE_PRIORITY) and
// not just "whichever row came first". Symbol order across the fixture is
// ETH, BTC, BTC, ETH, BTC — groups must come out in ETH-then-BTC
// first-appearance order, not alphabetical/count order.
const groupRows = [
  { symbol: 'ETH', row_type: 'liquidation', label: 'liq eth' },
  { symbol: 'BTC', row_type: 'zone', label: 'zone btc' },
  { symbol: 'BTC', row_type: 'position', label: 'pos btc' },
  { symbol: 'ETH', row_type: 'zone', label: 'zone eth' },
  { symbol: 'BTC', row_type: 'signal', label: 'sig btc' },
];
const groups = groupBySymbol(groupRows);

if (groups.length !== 2) fail(`expected 2 groups, got ${groups.length}`);

if (groups[0].symbol !== 'ETH') fail(`expected first group ETH (first-appearance order), got "${groups[0].symbol}"`);
if (groups[0].rows.length !== 2) fail(`expected ETH group to have 2 rows, got ${groups[0].rows.length}`);
// ETH has liquidation (priority 2) + zone (priority 3) -> liquidation wins -> tag LIQ.
if (groups[0].topTag !== 'LIQ') fail(`expected ETH group topTag "LIQ", got "${groups[0].topTag}"`);

if (groups[1].symbol !== 'BTC') fail(`expected second group BTC, got "${groups[1].symbol}"`);
if (groups[1].rows.length !== 3) fail(`expected BTC group to have 3 rows, got ${groups[1].rows.length}`);
// BTC has zone(3) + position(5) + signal(1), added in that array order -> signal (highest
// priority, lowest number) must still win even though it's added last -> tag SIGNAL.
if (groups[1].topTag !== 'SIGNAL') fail(`expected BTC group topTag "SIGNAL", got "${groups[1].topTag}"`);

// --- matchesTab (slice B, type tabs) --------------------------------------
const tabFixtureRows = {
  signal: { symbol: 'BTC', row_type: 'signal' },
  transition: { symbol: 'BTC', row_type: 'transition' },
  zone: { symbol: 'BTC', row_type: 'zone' },
  liquidation: { symbol: 'BTC', row_type: 'liquidation' },
  event: { symbol: 'MACRO', row_type: 'event' },
  position: { symbol: 'BTC', row_type: 'position' },
};
const allRowTypes = Object.keys(tabFixtureRows);

// 'all' is the EVENT feed — every type except static zone rows.
allRowTypes.forEach((rt) => {
  const want = rt !== 'zone';
  if (matchesTab(tabFixtureRows[rt], 'all') !== want) {
    fail(`expected tab "all" match for row_type "${rt}" to be ${want}`);
  }
});

// Each single-type tab matches only its own row_type.
const singleTypeTabs = { signals: 'signal', transitions: 'transition', zones: 'zone', liq: 'liquidation' };
Object.keys(singleTypeTabs).forEach((tab) => {
  const wantType = singleTypeTabs[tab];
  allRowTypes.forEach((rt) => {
    const got = matchesTab(tabFixtureRows[rt], tab);
    const want = rt === wantType;
    if (got !== want) fail(`matchesTab(row_type="${rt}", tab="${tab}") = ${got}, expected ${want}`);
  });
});

// 'events' matches BOTH event and position, nothing else.
allRowTypes.forEach((rt) => {
  const got = matchesTab(tabFixtureRows[rt], 'events');
  const want = rt === 'event' || rt === 'position';
  if (got !== want) fail(`matchesTab(row_type="${rt}", tab="events") = ${got}, expected ${want}`);
});

console.log('desk-stream check OK');
