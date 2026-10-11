// tests/js/portfolio-charts.test.cjs
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function load() {
  const src = fs.readFileSync(
    path.join(__dirname, '../../static/js/portfolio-charts.js'), 'utf8');
  const sandbox = { window: {}, document: { getElementById: () => null },
                    setTimeout, clearTimeout, console };
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return sandbox.window.PortfolioCharts;
}

function seriesByName(opt, name) {
  return opt.series.find(function(s) { return s.name === name; });
}

test('curve option plots book value and cost basis lines with their raw data', () => {
  const PC = load();
  const opt = PC.buildCurveOption([
    { date: '2026-01-01', book_value: 200, cost_basis: 200, excluded_count: 0 },
    { date: '2026-01-02', book_value: 220, cost_basis: 200, excluded_count: 0 },
  ]);
  assert.deepStrictEqual(opt.xAxis.data, ['2026-01-01', '2026-01-02']);
  assert.deepStrictEqual(seriesByName(opt, 'Book value').data, [200, 220]);
  assert.deepStrictEqual(seriesByName(opt, 'Cost basis').data, [200, 200]);
});

test('the raw book value and cost basis lines carry no area fill of their own', () => {
  // The fill lives entirely in the profit/loss band series, not in the
  // raw lines (book value must not fill itself down to the axis baseline).
  const PC = load();
  const opt = PC.buildCurveOption([
    { date: '2026-01-01', book_value: 200, cost_basis: 100, excluded_count: 0 },
  ]);
  assert.ok(!seriesByName(opt, 'Book value').areaStyle, 'book value has no area of its own');
  assert.ok(!seriesByName(opt, 'Cost basis').areaStyle, 'cost basis is a bare line');
});

test('curve option survives an empty curve', () => {
  const PC = load();
  const opt = PC.buildCurveOption([]);
  assert.deepStrictEqual(opt.xAxis.data, []);
  assert.deepStrictEqual(seriesByName(opt, 'Book value').data, []);
  assert.deepStrictEqual(seriesByName(opt, 'Cost basis').data, []);
  assert.deepStrictEqual(seriesByName(opt, '_pnlProfit').data, []);
  assert.deepStrictEqual(seriesByName(opt, '_pnlLoss').data, []);
});

test('a wholly profitable curve fills the gap in green only', () => {
  const PC = load();
  const opt = PC.buildCurveOption([
    { date: '2026-01-01', book_value: 300, cost_basis: 200, excluded_count: 0 },
    { date: '2026-01-02', book_value: 320, cost_basis: 200, excluded_count: 0 },
    { date: '2026-01-03', book_value: 310, cost_basis: 200, excluded_count: 0 },
  ]);
  assert.deepStrictEqual(seriesByName(opt, '_pnlProfit').data, [100, 120, 110]);
  assert.deepStrictEqual(seriesByName(opt, '_pnlLoss').data, [0, 0, 0]);
  assert.strictEqual(seriesByName(opt, '_pnlProfit').areaStyle.color, '#3fb950');
});

test('a wholly underwater curve fills the gap in red only', () => {
  const PC = load();
  const opt = PC.buildCurveOption([
    { date: '2026-01-01', book_value: 150, cost_basis: 200, excluded_count: 0 },
    { date: '2026-01-02', book_value: 140, cost_basis: 200, excluded_count: 0 },
    { date: '2026-01-03', book_value: 145, cost_basis: 200, excluded_count: 0 },
  ]);
  assert.deepStrictEqual(seriesByName(opt, '_pnlProfit').data, [0, 0, 0]);
  assert.deepStrictEqual(seriesByName(opt, '_pnlLoss').data, [50, 60, 55]);
  assert.strictEqual(seriesByName(opt, '_pnlLoss').areaStyle.color, '#ef5350');
});

test('a curve that crosses from profit into loss flips the band at the crossing point, not before or after', () => {
  const PC = load();
  // book stays above cost (200) for the first two points, then drops below
  // it for the last two — the crossing happens strictly between index 1 and 2.
  const opt = PC.buildCurveOption([
    { date: '2026-01-01', book_value: 220, cost_basis: 200, excluded_count: 0 },
    { date: '2026-01-02', book_value: 210, cost_basis: 200, excluded_count: 0 },
    { date: '2026-01-03', book_value: 190, cost_basis: 200, excluded_count: 0 },
    { date: '2026-01-04', book_value: 180, cost_basis: 200, excluded_count: 0 },
  ]);
  const profit = seriesByName(opt, '_pnlProfit').data;
  const loss = seriesByName(opt, '_pnlLoss').data;
  const baseline = seriesByName(opt, '_pnlBaseline').data;

  assert.deepStrictEqual(profit, [20, 10, 0, 0], 'profit band only where book >= cost');
  assert.deepStrictEqual(loss, [0, 0, 10, 20], 'loss band only where book < cost');
  assert.deepStrictEqual(baseline, [200, 200, 190, 180], 'floor is min(book, cost) at every point');

  // The two bands must never both be nonzero at the same index — a real
  // portfolio is never simultaneously "up" and "down" at the same instant,
  // and if both fired the stacked areas would silently double-count.
  profit.forEach(function(p, i) {
    assert.strictEqual(p * loss[i], 0, 'profit and loss are mutually exclusive at index ' + i);
  });
});

test('curve legend lists Book value and Cost basis, not the internal band series', () => {
  const PC = load();
  const opt = PC.buildCurveOption([
    { date: '2026-01-01', book_value: 220, cost_basis: 200, excluded_count: 0 },
  ]);
  assert.ok(opt.legend && opt.legend.show, 'legend is present');
  // Array.from re-materializes this in the test's own realm — legend.data
  // is a literal built inside the vm sandbox, so a bare deepStrictEqual
  // against a test-file array literal would fail on cross-realm prototype
  // identity alone despite identical content (see buildPnlBand's history:
  // the same class of harness artifact, not a logic bug).
  assert.deepStrictEqual(Array.from(opt.legend.data), ['Book value', 'Cost basis']);
  opt.legend.data.forEach(function(name) {
    assert.ok(!name.startsWith('_'), 'no internal helper series leaks into the legend');
  });
});

test('donut collapses a long tail into Other', () => {
  const PC = load();
  const rows = [];
  for (let i = 0; i < 12; i++) {
    rows.push({ asset: 'A' + i, value: 100 - i, weight_pct: 1 });
  }
  const opt = PC.buildDonutOption(rows);
  const names = opt.series[0].data.map(function(d) { return d.name; });
  assert.strictEqual(names.length, 9, 'eight slices plus Other');
  assert.strictEqual(names[8], 'Other');
  const other = opt.series[0].data[8].value;
  assert.strictEqual(other, 100 - 8 + (100 - 9) + (100 - 10) + (100 - 11));
});

test('donut leaves a short list alone', () => {
  const PC = load();
  const opt = PC.buildDonutOption([
    { asset: 'BTC', value: 30000, weight_pct: 98.68 },
    { asset: 'ETH', value: 400, weight_pct: 1.32 },
  ]);
  assert.deepStrictEqual(
    opt.series[0].data.map(function(d) { return d.name; }), ['BTC', 'ETH']);
});

test('donut option survives an empty allocation', () => {
  const PC = load();
  assert.deepStrictEqual(PC.buildDonutOption([]).series[0].data, []);
});
