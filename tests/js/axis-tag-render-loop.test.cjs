/* The y-axis price tag must follow the price line, not the 1s timer.
 *
 * The dashed CARRAC_LAST_PRICE line repositions on every chart repaint
 * (draw() → yAxis.convertToPixel), but the DOM tag was positioned only by the
 * 1s countdown interval — and that interval silently KEEPS the stale top when
 * chart.convertToPixel returns an empty object, while still updating the text.
 * Prod screenshot 2026-07-28: correct price text, tag ~140px above the line.
 *
 * Fix under test: countdown-timer.js exports window.carracPositionAxisTag(y),
 * and CARRAC_LAST_PRICE draw() calls it with the line's own y every repaint
 * (active pane only), so tag and line can never disagree for more than one
 * frame. Uses the shared VM patterns of axis-price-staleness.test.cjs
 * (countdown harness) and alert-line-axis-tag.test.cjs (source slice).
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const BAR_MS = 3600000; // 1h

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

/** Countdown-timer sandbox with a chart whose convertToPixel is scriptable per call. */
function countdownHarness() {
  const clock = Date.UTC(2026, 6, 27, 12, 0, 0);
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

  // fresh (forming) 1h bar + phantom padding, so no staleness paths interfere
  const lastTs = clock - 10 * 60000;
  const dl = [];
  for (let k = 3; k >= 1; k--) {
    dl.push({ timestamp: lastTs - BAR_MS * k, open: 64000, high: 64000, low: 64000, close: 64000, volume: 1 });
  }
  dl.push({ timestamp: lastTs, open: 64000, high: 64100, low: 64000, close: 64100, volume: 1 });
  for (let i = 1; i <= 20; i++) {
    dl.push({ timestamp: lastTs + BAR_MS * i, open: 64100, high: 64100, low: 64100, close: 64100, _phantom: true });
  }

  const chart = {
    _pixel: { y: 100 },              // next convertToPixel result — tests overwrite this
    getDataList: () => dl,
    convertToPixel() { return this._pixel; }
  };

  const RealDate = Date;
  function FakeDate(...a) { return a.length ? new RealDate(...a) : new RealDate(clock); }
  FakeDate.now = () => clock;
  FakeDate.UTC = RealDate.UTC;
  FakeDate.prototype = RealDate.prototype;

  const sandbox = {
    document, Date: FakeDate, Math, JSON, String, isFinite, console,
    klineChart: chart, klineChartSymbol: 'BTC', klineChartTimeframe: '1h',
    _logScaleEnabled: false,
    loadKlineChart() {}, klineDataCache: {},
    sessionStorage: { removeItem() {}, getItem: () => null, setItem() {} }
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(
    fs.readFileSync(path.join(ROOT, 'static/js/countdown-timer.js'), 'utf8'),
    sandbox, { filename: 'countdown-timer.js' }
  );
  return { sandbox, chart, document };
}

// ── countdown-timer.js: the render-loop setter ──────────────────────

test('carracPositionAxisTag moves the badge and republishes the tag rect', () => {
  const { sandbox, document } = countdownHarness();
  sandbox.updateCandleCountdown();               // creates the badge at y=100
  const el = document.getElementById('kline-candle-countdown');
  assert.ok(el, 'badge exists');
  assert.strictEqual(el.style.top, '92px');      // 100 - 16/2

  assert.strictEqual(typeof sandbox.carracPositionAxisTag, 'function',
    'countdown-timer.js must export window.carracPositionAxisTag');
  sandbox.carracPositionAxisTag(240);
  assert.strictEqual(el.style.top, '232px', 'render-loop call repositions the tag');
  assert.ok(sandbox._carracAxisTagRect && Math.abs(sandbox._carracAxisTagRect.y - 232) < 1,
    'occupied rect follows the tag');
});

test('empty convertToPixel result leaves a stale top — the render loop must correct it', () => {
  const { sandbox, chart, document } = countdownHarness();
  sandbox.updateCandleCountdown();
  const el = document.getElementById('kline-candle-countdown');
  assert.strictEqual(el.style.top, '92px');

  // klinecharts v9 chart.convertToPixel returns {} when the pane lookup or the
  // value check fails — the timer tick then updates the TEXT but silently
  // keeps the old top. This is the observed production symptom.
  chart._pixel = {};
  sandbox.updateCandleCountdown();
  assert.strictEqual(el.style.top, '92px', 'timer tick retained the stale top');
  assert.notStrictEqual(el.style.display, 'none', 'tag still visible at the wrong y');

  // The fix: the line's draw() pushes its own y through the setter each repaint.
  sandbox.carracPositionAxisTag(310);
  assert.strictEqual(el.style.top, '302px', 'render loop recovers the tag');
});

test('setter is a safe no-op before the badge exists and on bad input', () => {
  const { sandbox } = countdownHarness();
  assert.doesNotThrow(() => {
    sandbox.carracPositionAxisTag(120);   // no badge element yet
  });
  sandbox.updateCandleCountdown();
  const el = sandbox.document.getElementById('kline-candle-countdown');
  assert.doesNotThrow(() => {
    sandbox.carracPositionAxisTag(null);
    sandbox.carracPositionAxisTag(NaN);
  });
  assert.strictEqual(el.style.top, '92px', 'bad input must not move the tag');
});

// ── chart-indicators.js: CARRAC_LAST_PRICE draw() drives the setter ─

const indicatorsSrc = fs.readFileSync(path.join(ROOT, 'static/js/chart-indicators.js'), 'utf8');

function sliceIndicators(startMarker, endMarker) {
  const start = indicatorsSrc.indexOf(startMarker);
  assert.ok(start >= 0, `marker not found: ${startMarker}`);
  const end = indicatorsSrc.indexOf(endMarker, start);
  assert.ok(end > start, `end marker not found: ${endMarker}`);
  return indicatorsSrc.slice(start, end);
}

function loadLastPriceIndicator(opts) {
  opts = opts || {};
  const captured = {};
  const dl = [
    { timestamp: 1000, open: 64000, high: 64100, low: 63900, close: 64050 },
    { timestamp: 2000, open: 64050, high: 64200, low: 64000, close: 64150 }
  ];
  const env = {
    klinecharts: { registerIndicator: (def) => { captured[def.name] = def; } },
    klineChart: { getDataList: () => dl },
    klineChartSymbol: 'BTC', klineChartTimeframe: '1h',
    console, Math, JSON, Date,
    positioned: []
  };
  env.window = env;
  if (opts.withSetter !== false) {
    env.carracPositionAxisTag = (y) => { env.positioned.push(y); };
  }
  const code = sliceIndicators('var _lastPriceIndicatorRegistered', 'function applyKlineIndicators');
  new vm.Script(code, { filename: 'last-price-slice.js' }).runInContext(vm.createContext(env));
  env._ensureLastPriceIndicator();
  assert.ok(captured.CARRAC_LAST_PRICE, 'CARRAC_LAST_PRICE registered');
  return { def: captured.CARRAC_LAST_PRICE, env, dl };
}

function drawParams(dl, yPixel) {
  const calls = [];
  return {
    calls,
    params: {
      ctx: {
        save() {}, restore() {}, beginPath() {}, moveTo() {}, lineTo() {},
        stroke() { calls.push('stroke'); }, setLineDash() {},
        set strokeStyle(v) {}, set lineWidth(v) {}
      },
      bounding: { width: 900, height: 600 },
      kLineDataList: dl,
      yAxis: { convertToPixel: () => yPixel }
    }
  };
}

test('draw() publishes the line y to carracPositionAxisTag on the active pane', () => {
  const { def, env, dl } = loadLastPriceIndicator();
  const { params, calls } = drawParams(dl, 240);
  def.draw(params);
  assert.ok(calls.includes('stroke'), 'line was drawn');
  assert.deepStrictEqual(env.positioned, [240],
    'draw() must reposition the DOM tag with the SAME y as the line');
});

test('draw() does not touch the main tag when drawing a companion pane', () => {
  const { def, env } = loadLastPriceIndicator();
  const otherDl = [
    { timestamp: 1000, open: 100, high: 110, low: 95, close: 105 },
    { timestamp: 2000, open: 105, high: 115, low: 100, close: 108 }
  ];
  const { params } = drawParams(otherDl, 240);  // dataList !== klineChart.getDataList()
  def.draw(params);
  assert.deepStrictEqual(env.positioned, [],
    'a companion pane must not move the active pane tag');
});

test('draw() survives the setter being absent (standalone pages)', () => {
  const { def, dl } = loadLastPriceIndicator({ withSetter: false });
  const { params, calls } = drawParams(dl, 240);
  assert.doesNotThrow(() => def.draw(params));
  assert.ok(calls.includes('stroke'), 'line still drawn without the setter');
});
