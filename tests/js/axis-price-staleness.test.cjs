/* The y-axis price tag must not render a frozen candle close.
 *
 * Background: chartTickDecision (chart-live-tick.js case B) deliberately stops
 * applying live ticks once the last real bar's period has elapsed — writing
 * into a closed candle would corrupt its OHLC. It schedules a reload instead.
 * Until that reload lands fresh data the bar's close is frozen, so the axis
 * tag shows the live feed price instead of the frozen close (a 5m chart on a
 * payload ~8 bar periods old would otherwise update only once per 30s reload).
 *
 * These tests drive the REAL countdown-timer.js against a model of klinecharts'
 * data store whose semantics were read out of the bundled v9.8.12 build:
 *   getDataList() returns the internal array BY REFERENCE
 *   addData(item): l > c => push, l === c => replace tail object, l < c => no-op
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const TF_MS = { '1m': 60000, '5m': 300000, '1h': 3600000, '1D': 86400000 };

function makeEl(tag) {
  const el = {
    tagName: tag, id: '', className: '', style: {}, parentNode: null, children: [],
    _text: '', offsetHeight: 16, offsetWidth: 78,
    get lastElementChild() { return this.children[this.children.length - 1] || null; },
    get textContent() {
      return this.children.length ? this.children.map(c => c.textContent).join('') : this._text;
    },
    set textContent(v) { this._text = String(v); this.children = []; },
    set innerHTML(v) {
      this.children = [];
      (String(v).match(/class="([a-z-]+)"/g) || []).forEach(m => {
        const c = makeEl('div');
        c.className = m.slice(7, -1);
        c.parentNode = this;
        this.children.push(c);
        el._reg.push(c);
      });
    },
    appendChild(c) {
      if (c.parentNode) c.parentNode.children = c.parentNode.children.filter(x => x !== c);
      c.parentNode = this; this.children.push(c); return c;
    },
    remove() {
      if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(x => x !== this);
      this.parentNode = null;
    },
    querySelector(s) { return this.children.find(c => '.' + c.className === s) || null; },
    querySelectorAll(s) { return this.children.filter(c => '.' + c.className === s); }
  };
  return el;
}

/** Fresh sandbox with countdown-timer.js loaded and a controllable clock. */
function harness(opts) {
  const tf = opts.tf;
  const barMs = TF_MS[tf];
  let clock = Date.UTC(2026, 6, 27, 12, 0, 0);

  const reg = [];
  const container = makeEl('div');
  container.id = 'kline-chart-container';
  container._reg = reg;
  reg.push(container);
  const wrap = makeEl('div'), pane = makeEl('div'), yaxis = makeEl('div');
  wrap._reg = pane._reg = yaxis._reg = reg;
  container.appendChild(wrap); wrap.appendChild(pane); pane.appendChild(yaxis);
  const track = el => { el._reg = reg; if (!reg.includes(el)) reg.push(el); return el; };

  const document = {
    hidden: false,
    getElementById: id => reg.find(e => e.id === id) || null,
    createElement: t => track(makeEl(t)),
    querySelectorAll: s => reg.filter(e => '.' + e.className === s),
    addEventListener() {}
  };

  // last real bar is `barAgeMs` old; phantom padding follows it
  const lastTs = clock - opts.barAgeMs;
  const dl = [];
  for (let k = 3; k >= 1; k--) {
    dl.push({ timestamp: lastTs - barMs * k, open: opts.close, high: opts.close, low: opts.close, close: opts.close, volume: 1 });
  }
  dl.push({ timestamp: lastTs, open: opts.open, high: opts.close, low: opts.open, close: opts.close, volume: 1 });
  for (let i = 1; i <= 120; i++) {
    dl.push({ timestamp: lastTs + barMs * i, open: opts.close, high: opts.close, low: opts.close, close: opts.close, _phantom: true });
  }

  let pixelArg = null;
  const chart = {
    getDataList: () => dl,
    convertToPixel(v) { pixelArg = v.value; return { y: 100 }; }
  };

  const RealDate = Date;
  function FakeDate(...a) { return a.length ? new RealDate(...a) : new RealDate(clock); }
  FakeDate.now = () => clock;
  FakeDate.UTC = RealDate.UTC;
  FakeDate.prototype = RealDate.prototype;

  const sandbox = {
    document, Date: FakeDate, Math, JSON, String, isFinite, console,
    klineChart: chart, klineChartSymbol: 'BTC', klineChartTimeframe: tf,
    _logScaleEnabled: !!opts.logScale,
    allAssets: opts.feed == null ? undefined : { BTC: { latest_price: opts.feed } },
    loadKlineChart() {}, klineDataCache: {},
    sessionStorage: { removeItem() {}, getItem: () => null, setItem() {} }
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(
    fs.readFileSync(path.join(ROOT, 'static/js/countdown-timer.js'), 'utf8'),
    sandbox, { filename: 'countdown-timer.js' }
  );

  sandbox.updateCandleCountdown();
  const badgeEl = document.getElementById('kline-candle-countdown');
  const priceRow = badgeEl && badgeEl.querySelector('.price-row');
  return {
    badge: priceRow ? parseFloat(priceRow.textContent.replace(/,/g, '')) : null,
    colour: priceRow ? priceRow.style.backgroundColor : null,
    pixelArg,
    axis: sandbox.carracAxisPrice(dl[3], tf, 'BTC')
  };
}

test('forming bar: tag shows the candle close, feed is ignored', () => {
  // ticks own a forming bar — substituting the feed here would fight the tick path
  const h = harness({ tf: '1h', barAgeMs: 10 * 60000, open: 64000, close: 64100, feed: 64950 });
  assert.strictEqual(h.badge, 64100);
  assert.strictEqual(h.axis.stale, false);
});

test('bar older than its period: tag shows the live feed, not the frozen close', () => {
  // 5m chart on a payload ~8 bar periods old
  const h = harness({ tf: '5m', barAgeMs: 2379 * 1000, open: 64000, close: 64100, feed: 64950 });
  assert.strictEqual(h.axis.stale, true);
  assert.strictEqual(h.badge, 64950, 'must not render the frozen candle close');
});

test('stale bar on the daily timeframe is covered too', () => {
  const h = harness({ tf: '1D', barAgeMs: 25 * 3600 * 1000, open: 64000, close: 64100, feed: 63200 });
  assert.strictEqual(h.badge, 63200);
});

test('no feed price available: falls back to the candle close', () => {
  // standalone chart page has no allAssets — must never be worse than before
  const h = harness({ tf: '5m', barAgeMs: 2379 * 1000, open: 64000, close: 64100, feed: null });
  assert.strictEqual(h.badge, 64100);
});

test('tag colour follows the price actually shown', () => {
  // frozen close (64100) is above the open, but the live feed is below it —
  // colouring by the stale close would paint a green tag on a falling price
  const h = harness({ tf: '5m', barAgeMs: 2379 * 1000, open: 64000, close: 64100, feed: 63500 });
  assert.strictEqual(h.badge, 63500);
  assert.strictEqual(h.colour, '#ef5350');
});

test('log scale: tag prints a linear price, positions in log space', () => {
  // convertToKlineData stores Math.log(c) in close under log scale, so the old
  // code printed ~11.07 instead of a price on every log-scale chart
  const h = harness({
    tf: '1h', barAgeMs: 10 * 60000,
    open: Math.log(64000), close: Math.log(64100), feed: null, logScale: true
  });
  assert.ok(Math.abs(h.badge - 64100) < 1, 'expected a linear price, got ' + h.badge);
  assert.ok(Math.abs(h.pixelArg - Math.log(64100)) < 1e-9, 'must position in chart (log) space');
});

test('log scale + stale bar: feed price is converted back to log for positioning', () => {
  const h = harness({
    tf: '5m', barAgeMs: 2379 * 1000,
    open: Math.log(64000), close: Math.log(64100), feed: 64950, logScale: true
  });
  assert.ok(Math.abs(h.badge - 64950) < 1);
  assert.ok(Math.abs(h.pixelArg - Math.log(64950)) < 1e-9);
});
