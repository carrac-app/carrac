/**
 * Position overlay painter — R multiples on TP labels, log-safe label prices,
 * and full-width guideline lines while _positionGuidelinesFor is set.
 * The painter lives in its own file,
 * static/js/chart-position-overlays.js, so the whole file loads into the VM —
 * no marker slicing needed (VM pattern shared with alert-line-axis-tag.test.cjs).
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createContext, Script } = require('node:vm');
const path = require('node:path');

const src = readFileSync(path.join(__dirname, '../../static/js/chart-position-overlays.js'), 'utf8');

function slice(startMarker, endMarker) {
    const start = src.indexOf(startMarker);
    assert.ok(start >= 0, `marker not found: ${startMarker}`);
    const end = src.indexOf(endMarker, start);
    assert.ok(end > start, `end marker not found: ${endMarker}`);
    return src.slice(start, end);
}

function loadPainter(opts) {
    opts = opts || {};
    const env = {
        _fmtPriceSmart: (p) => {
            var abs = Math.abs(p);
            var s = abs >= 1000 ? p.toFixed(2) : (abs >= 1 ? p.toFixed(2) : (abs >= 0.01 ? p.toFixed(4) : p.toFixed(6)));
            var parts = s.split('.');
            parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
            return parts.join('.');
        },
        carracFromChartSpace: (v) => (v == null || !isFinite(v)) ? null : (opts.logScale ? Math.exp(v) : v),
        console, Math, JSON, Object, Array, isFinite, parseFloat, parseInt,
    };
    env.window = env;
    if (opts.guidelinesFor) env._positionGuidelinesFor = opts.guidelinesFor;
    new Script(src, { filename: 'chart-position-overlays.js' }).runInContext(createContext(env));
    assert.equal(typeof env.createPositionFigures, 'function', 'createPositionFigures sliced');
    assert.equal(typeof env.carracPositionStats, 'function', 'carracPositionStats sliced');
    return env;
}

// entry 100, SL 90, TP 130, one intermediate TP at 110 (long)
const LONG = (extra) => Object.assign({
    coordinates: [{ x: 100, y: 300 }, { x: 100, y: 400 }, { x: 100, y: 100 }, { x: 250, y: 300 }],
    bounding: { width: 900, height: 600 },
    overlay: {
        id: 'pos_1', paneId: 'candle_pane',
        points: [{ value: 100, timestamp: 1000 }, { value: 90, timestamp: 1000 }, { value: 130, timestamp: 1000 }],
        extendData: { tps: [110] },
    },
}, extra || {});

const texts = (figs) => figs.filter(f => f.type === 'text').map(f => f.attrs.text);
const lines = (figs) => figs.filter(f => f.type === 'line');

test('carracPositionStats: long R math from raw prices', () => {
    const env = loadPainter();
    const s = env.carracPositionStats(130, 100, 90);
    assert.equal(s.r, 3);
    assert.equal(s.rLabel, '3R');
    assert.ok(Math.abs(s.pct - 30) < 1e-9);
});

test('carracPositionStats: short direction + fractional R label', () => {
    const env = loadPainter();
    const s = env.carracPositionStats(85, 100, 110); // short: entry 100, SL 110
    assert.equal(s.rLabel, '1.5R');
    assert.ok(Math.abs(s.pct - 15) < 1e-9);
});

test('carracPositionStats: entry==SL guard hides R', () => {
    const env = loadPainter();
    const s = env.carracPositionStats(130, 100, 100);
    assert.equal(s.r, null);
    assert.equal(s.rLabel, '');
});

test('TP labels carry R multiple', () => {
    const env = loadPainter();
    const figs = env.createPositionFigures(LONG());
    const t = texts(figs);
    assert.ok(t.some(x => x.includes('TP1') && x.includes('+10.00%') && x.includes('· 1R')), 'itp label has R: ' + JSON.stringify(t));
    assert.ok(t.some(x => x.includes('TP2') && x.includes('+30.00%') && x.includes('· 3R')), 'final TP label has R: ' + JSON.stringify(t));
});

test('log scale: labels show raw prices, not ln values', () => {
    const env = loadPainter({ logScale: true });
    const p = LONG();
    p.overlay.points = p.overlay.points.map(pt => ({ value: Math.log(pt.value), timestamp: pt.timestamp }));
    p.overlay.extendData = { tps: [Math.log(110)] };
    const figs = env.createPositionFigures(p);
    const t = texts(figs);
    assert.ok(t.some(x => x.includes('100.00')), 'entry label shows raw 100: ' + JSON.stringify(t));
    assert.ok(t.some(x => x.includes('· 3R')), 'R computed from raw prices: ' + JSON.stringify(t));
});

// LONG fixture: xLeft=100, xRight=250, bounding.width (xEdge)=900.
test('guideline flag: bounded box stays full strength, extensions are faded', () => {
    const envOn = loadPainter({ guidelinesFor: 'pos_1' });
    const figsOn = envOn.createPositionFigures(LONG());
    const allLines = lines(figsOn);

    // (i) bounded [100→250] segments present, in their original (unfaded) styles.
    const bounded = allLines.filter(l =>
        l.attrs.coordinates[0].x === 100 && l.attrs.coordinates[1].x === 250);
    assert.ok(bounded.length >= 4, 'entry+SL+TP+itp bounded segments present, got ' + bounded.length);
    assert.ok(bounded.some(l => l.styles.color === '#d1d4dc'), 'entry bounded keeps original color');
    assert.ok(bounded.some(l => l.styles.color === '#AB47BC'), 'SL bounded keeps original color');
    assert.ok(bounded.some(l => l.styles.color === '#FFD700'), 'TP bounded keeps original color');
    assert.ok(bounded.some(l => l.styles.color === 'rgba(255, 215, 0, 0.6)'), 'itp bounded keeps original color');

    // (ii) extension segments [0→100] and [250→900] present, faded (alpha < 0.5).
    const leftExt = allLines.filter(l => l.attrs.coordinates[0].x === 0 && l.attrs.coordinates[1].x === 100);
    const rightExt = allLines.filter(l => l.attrs.coordinates[0].x === 250 && l.attrs.coordinates[1].x === 900);
    assert.equal(leftExt.length, 4, 'entry+SL+TP+itp left extensions, got ' + leftExt.length);
    assert.equal(rightExt.length, 4, 'entry+SL+TP+itp right extensions, got ' + rightExt.length);
    leftExt.concat(rightExt).forEach(l => {
        const m = /rgba\([^)]*,\s*([\d.]+)\s*\)/.exec(l.styles.color);
        assert.ok(m, 'extension color is an rgba string: ' + l.styles.color);
        assert.ok(parseFloat(m[1]) < 0.5, 'extension alpha < 0.5: ' + l.styles.color);
        // (iii) extensions have ignoreEvent true
        assert.equal(l.ignoreEvent, true, 'extension has ignoreEvent true');
    });

    // Flag off: EXACTLY today's behavior — no segment touches x=0 at all.
    const envOff = loadPainter();
    const figsOff = envOff.createPositionFigures(LONG());
    const touchesZero = lines(figsOff).filter(l =>
        l.attrs.coordinates[0].x === 0 || l.attrs.coordinates[1].x === 0);
    assert.equal(touchesZero.length, 0, 'no segments touch x=0 when flag off, got ' + touchesZero.length);
});

test('Y-axis price tags are log-safe', () => {
    const env = loadPainter({ logScale: true });
    const p = LONG();
    p.overlay.points = p.overlay.points.map(pt => ({ value: Math.log(pt.value), timestamp: pt.timestamp }));
    p.overlay.extendData = {};
    const figs = env.createPositionYAxisFigures(p);
    const t = texts(figs);
    assert.ok(t.some(x => x.includes('100.0000') || x.includes('100.00')), 'y-axis entry tag raw: ' + JSON.stringify(t));
});
