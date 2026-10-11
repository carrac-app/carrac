/**
 * Alert overlays — 'alertLine' (two-click bounded segment, the original form)
 * and 'alertLevel' (one-click infinite level). Both carry a Y-axis price tag
 * (log-safe via carracFromChartSpace), follow the picked color, render
 * extendData.note, and nudge the tag clear of the DOM last-price tag.
 *
 * Slices both registrations out of static/js/chart-overlay-defs.js (VM pattern shared with
 * scenario-path-figures.test.cjs / measure-tool-label.test.cjs).
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createContext, Script } = require('node:vm');
const path = require('node:path');

const src = readFileSync(path.join(__dirname, '../../static/js/chart-overlay-defs.js'), 'utf8');

function slice(startMarker, endMarker) {
    const start = src.indexOf(startMarker);
    assert.ok(start >= 0, `marker not found: ${startMarker}`);
    const end = src.indexOf(endMarker, start);
    assert.ok(end > start, `end marker not found: ${endMarker}`);
    return src.slice(start, end);
}

function loadAlertDefs(opts) {
    opts = opts || {};
    const captured = {};
    const env = {
        klinecharts: { registerOverlay: (def) => { captured[def.name] = def; } },
        _selectionSquares: () => [],
        allAssets: { BTC: { latest_price: opts.livePrice != null ? opts.livePrice : 100000 } },
        klineChartSymbol: 'BTC',
        klineSelectedOverlayId: opts.selectedId || null,
        _hoveredOverlayId: null,
        _logScaleEnabled: !!opts.logScale,
        console, Math, JSON, Object, Array,
    };
    env.window = env;
    env.carracFromChartSpace = (v) => (v == null || !isFinite(v)) ? null : (env._logScaleEnabled ? Math.exp(v) : v);
    if (opts.axisTagRect) env._carracAxisTagRect = opts.axisTagRect;
    const code = slice('// Alert line overlay', '// Register milestonePin overlay');
    new Script(code, { filename: 'alert-overlays-slice.js' }).runInContext(createContext(env));
    assert.ok(captured.alertLine, 'alertLine registered');
    assert.ok(captured.alertLevel, 'alertLevel registered');
    return { defs: captured, env };
}

const SEG = (value, y) => ({
    coordinates: [{ x: 120, y: y != null ? y : 300 }, { x: 240, y: y != null ? y : 300 }],
    bounding: { width: 900, height: 600 },
    overlay: { id: 'al_1', points: [{ value, timestamp: 1000 }, { value, timestamp: 2000 }], extendData: { _color: '#26a69a' } },
});
const LVL = (value, y) => ({
    coordinates: [{ x: 120, y: y != null ? y : 300 }],
    bounding: { width: 900, height: 600 },
    overlay: { id: 'lv_1', points: [{ value, timestamp: 1000 }], extendData: { _color: '#26a69a' } },
});

// ── alertLine: the restored bounded segment ─────────────────────────

test('alertLine renders a BOUNDED segment with endpoint dots (not infinite)', () => {
    const { defs } = loadAlertDefs();
    const figs = defs.alertLine.createPointFigures(SEG(118240));
    const lines = figs.filter((f) => f.type === 'line');
    lines.forEach((l) => {
        assert.equal(l.attrs.coordinates[0].x, 120, 'starts at first click');
        assert.equal(l.attrs.coordinates[1].x, 240, 'ends at second click');
    });
    assert.equal(figs.filter((f) => f.type === 'circle').length, 2, 'endpoint dots restored');
});

test('alertLine floating label is log-safe (real price, not ln)', () => {
    const { defs } = loadAlertDefs({ logScale: true });
    const figs = defs.alertLine.createPointFigures(SEG(Math.log(118240)));
    const label = figs.filter((f) => f.type === 'text').map((f) => f.attrs.text)
        .find((t) => t.indexOf('118240') >= 0);
    assert.ok(label, 'label shows the real price on log charts');
});

test('alertLine renders extendData.note', () => {
    const { defs } = loadAlertDefs();
    const p = SEG(118240);
    p.overlay.extendData.note = 'breakout watch';
    const texts = defs.alertLine.createPointFigures(p).filter((f) => f.type === 'text').map((f) => f.attrs.text);
    assert.ok(texts.indexOf('breakout watch') >= 0, `note rendered: ${JSON.stringify(texts)}`);
});

// ── alertLevel: the one-click infinite level ────────────────────────

test('alertLevel spans the full pane width from a single point', () => {
    const { defs } = loadAlertDefs();
    const figs = defs.alertLevel.createPointFigures(LVL(118240));
    const lines = figs.filter((f) => f.type === 'line');
    assert.ok(lines.length >= 1);
    lines.forEach((l) => {
        assert.equal(l.attrs.coordinates[0].x, 0);
        assert.equal(l.attrs.coordinates[1].x, 900);
    });
    assert.equal(figs.filter((f) => f.type === 'circle').length, 0, 'no stray endpoint handles');
});

test('alertLevel note renders in the line color', () => {
    const { defs } = loadAlertDefs();
    const p = LVL(118240);
    p.overlay.extendData.note = 'sweep zone';
    const txt = defs.alertLevel.createPointFigures(p).find((f) => f.type === 'text');
    assert.ok(txt && txt.attrs.text === 'sweep zone');
    assert.equal(txt.styles.color, '#26a69a');
});

// ── shared y-axis tag (both overlays use the same builder) ──────────

for (const which of ['alertLine', 'alertLevel']) {
    const P = which === 'alertLine' ? SEG : LVL;

    test(`${which}: y-axis tag shows the price`, () => {
        const { defs } = loadAlertDefs();
        const txt = defs[which].createYAxisFigures(P(118240)).find((f) => f.type === 'text');
        assert.ok(txt && txt.attrs.text.indexOf('118240') >= 0, `${txt && txt.attrs.text}`);
    });

    test(`${which}: y-axis tag is log-safe`, () => {
        const { defs } = loadAlertDefs({ logScale: true });
        const txt = defs[which].createYAxisFigures(P(Math.log(118240))).find((f) => f.type === 'text');
        assert.ok(txt && txt.attrs.text.indexOf('118240') >= 0 && txt.attrs.text.indexOf('11.6') === -1);
    });

    test(`${which}: tag follows the picked color and chevron is linear-vs-linear`, () => {
        const { defs } = loadAlertDefs({ logScale: true, livePrice: 100000 });
        const figs = defs[which].createYAxisFigures(P(Math.log(118240)));
        const rect = figs.find((f) => f.type === 'rect');
        const txt = figs.find((f) => f.type === 'text');
        assert.equal(rect.styles.color, '#26a69a');
        assert.ok(txt.attrs.text.indexOf('▲') >= 0, `up chevron: ${txt.attrs.text}`);
    });

    test(`${which}: all y-axis figures ignoreEvent; off-pane/degenerate return []`, () => {
        const { defs } = loadAlertDefs();
        defs[which].createYAxisFigures(P(118240)).forEach((f) => assert.equal(f.ignoreEvent, true));
        assert.equal(defs[which].createYAxisFigures(P(118240, -40)).length, 0, 'off-pane');
        assert.equal(defs[which].createYAxisFigures({ coordinates: [], bounding: { width: 900, height: 600 }, overlay: { points: [] } }).length, 0);
        assert.equal(defs[which].createPointFigures({ coordinates: [], bounding: { width: 900, height: 600 }, overlay: { points: [] } }).length, 0);
    });
}

test('tag nudges clear of the DOM last-price tag', () => {
    const { env } = loadAlertDefs();
    assert.equal(typeof env._alertAxisTagY, 'function');
    assert.equal(env._alertAxisTagY(300, null), 300);
    assert.ok(env._alertAxisTagY(300, { y: 295, h: 32 }) <= 295 - 8, 'nudged above');
    assert.ok(env._alertAxisTagY(315, { y: 295, h: 32 }) >= 295 + 32 + 8, 'nudged below');
    assert.equal(env._alertAxisTagY(100, { y: 295, h: 32 }), 100);
});
