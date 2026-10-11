/**
 * scenarioPath renderer — waypoint circles are scenario furniture.
 *
 * The hand-drawn Path tool reuses the 'scenarioPath' overlay. Its always-on
 * waypoint circles made a finished path look permanently selected. Rule:
 * circles render only for AI-scenario overlays (extendData.direction is
 * always stamped by scenarios.js); user-drawn paths get selection feedback
 * from _selectionSquares alone.
 *
 * The registration block is sliced out of static/js/chart-overlay-defs.js and evaluated in
 * a VM with a captured registerOverlay — same pattern as the toolbar tests.
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

function loadScenarioPathDef(envOverrides) {
    let captured = null;
    const env = Object.assign({
        klinecharts: { registerOverlay: (def) => { captured = def; } },
        hexToRgba: (hex, a) => `rgba(${hex},${a})`,
        _selectionSquares: () => [],
        klineSelectedOverlayId: null,
        _hoveredOverlayId: null,
        Math, JSON, Object, Array,
    }, envOverrides || {});
    env.window = env;
    const code = slice('// Register scenarioPath overlay', '// Register elliottWave overlay');
    new Script(code, { filename: 'scenarioPath-slice.js' }).runInContext(createContext(env));
    assert.ok(captured, 'registerOverlay was not called by the slice');
    return captured;
}

const COORDS = [{ x: 10, y: 40 }, { x: 60, y: 20 }, { x: 110, y: 55 }];

function figuresFor(extendData, envOverrides) {
    const def = loadScenarioPathDef(envOverrides);
    return def.createPointFigures({
        coordinates: COORDS,
        overlay: { id: 'ov_path_1', extendData },
    });
}

const circles = (figs) => figs.filter((f) => f.type === 'circle');
const lines = (figs) => figs.filter((f) => f.type === 'line');

test('user-drawn path (no direction): no waypoint circles when idle', () => {
    const figs = figuresFor({ _color: '#3fb950', _width: 2 });
    assert.equal(circles(figs).length, 0,
        'a finished Path drawing must not render permanent waypoint circles');
    assert.equal(lines(figs).length, COORDS.length - 1, 'segments still drawn');
});

test('AI scenario (direction stamped): waypoint circles stay', () => {
    const figs = figuresFor({ direction: 'bullish', name: 'Rally leg' });
    assert.equal(circles(figs).length, COORDS.length, 'scenario keeps its dots');
});

test('temp scenario while drawing (direction: neutral) keeps circles', () => {
    const figs = figuresFor({ direction: 'neutral', name: 'Drawing...', isTemp: true });
    assert.equal(circles(figs).length, COORDS.length);
});

test('user path still yields selection squares when selected', () => {
    let squaresAskedFor = null;
    const figs = figuresFor({ _color: '#3fb950', _width: 2 }, {
        klineSelectedOverlayId: 'ov_path_1',
        _selectionSquares: (coords, id) => {
            squaresAskedFor = id;
            return coords.map((c) => ({ type: 'rect', attrs: { x: c.x, y: c.y } }));
        },
    });
    assert.equal(squaresAskedFor, 'ov_path_1', 'selection squares consulted');
    assert.equal(figs.filter((f) => f.type === 'rect').length, COORDS.length);
});
