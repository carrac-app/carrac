/**
 * measureTool label — percentage, multiple (x), and log-scale correctness.
 *
 * Owner ask: show how many x the move is ("went up 2.5x") after the
 * percentage. Also: on log-scale charts overlay point values are ln-space,
 * so the price/% math must exp() back first (same bug class as the
 * log-scale y-axis tag fix).
 *
 * Slices the measureTool registration out of static/js/chart-overlay-defs.js (same VM
 * pattern as scenario-path-figures.test.cjs).
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

function loadMeasureDef(logScale) {
    let captured = null;
    const env = {
        klinecharts: { registerOverlay: (def) => { captured = def; } },
        klineChart: { getDataList: () => [{ timestamp: 1000 }, { timestamp: 2000 }] },
        _selectionSquares: () => [],
        _logScaleEnabled: !!logScale,
        console, Math, JSON, Object, Array,
    };
    env.window = env;
    const code = slice('// Measure tool overlay', '// Alert line overlay');
    new Script(code, { filename: 'measureTool-slice.js' }).runInContext(createContext(env));
    assert.ok(captured, 'registerOverlay not called');
    return captured;
}

function vertLabelFor(def, v1, v2) {
    const figs = def.createPointFigures({
        coordinates: [{ x: 0, y: 100 }, { x: 50, y: 0 }],
        overlay: {
            id: 'm1',
            points: [
                { value: v1, dataIndex: 0, timestamp: 1000 },
                { value: v2, dataIndex: 10, timestamp: 2000 },
            ],
        },
    });
    const texts = figs.filter((f) => f.type === 'text').map((f) => f.attrs.text);
    const label = texts.find((t) => t.indexOf('%') >= 0);
    assert.ok(label, 'vertical %-label figure exists');
    return label;
}

test('label shows the multiple after the percentage (100 -> 250 = 2.50x)', () => {
    const label = vertLabelFor(loadMeasureDef(false), 100, 250);
    assert.ok(label.indexOf('+150.00%') >= 0, `has pct: ${label}`);
    assert.ok(label.indexOf('2.50x') >= 0, `has multiple: ${label}`);
    assert.ok(label.indexOf('%') < label.indexOf('2.50x'), 'multiple comes after pct');
});

test('down moves show a sub-1 multiple (100 -> 40 = 0.40x)', () => {
    const label = vertLabelFor(loadMeasureDef(false), 100, 40);
    assert.ok(label.indexOf('-60.00%') >= 0, `has pct: ${label}`);
    assert.ok(label.indexOf('0.40x') >= 0, `has multiple: ${label}`);
});

test('log scale: values are ln-space and must exp() back before the math', () => {
    const label = vertLabelFor(loadMeasureDef(true), Math.log(100), Math.log(250));
    assert.ok(label.indexOf('+150.00%') >= 0, `pct from real prices, not ln: ${label}`);
    assert.ok(label.indexOf('2.50x') >= 0, `multiple from real prices: ${label}`);
});
