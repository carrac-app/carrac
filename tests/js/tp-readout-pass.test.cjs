/**
 * TP-zone hover readout — _tpReadoutPass must fire on hover for LOCKED
 * positions (they can never be selected by clicking the chart), and must
 * NOT show the '⌥click = TP' hint for them since Alt+click is unreachable.
 * Slices static/js/kline-drawings.js via string markers, VM pattern shared
 * with tests/js/position-figures-labels.test.cjs.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createContext, Script } = require('node:vm');
const path = require('node:path');

const src = readFileSync(path.join(__dirname, '../../static/js/kline-drawings.js'), 'utf8');

function slice(startMarker, endMarker) {
    const start = src.indexOf(startMarker);
    assert.ok(start >= 0, `marker not found: ${startMarker}`);
    const end = src.indexOf(endMarker, start);
    assert.ok(end > start, `end marker not found: ${endMarker}`);
    return src.slice(start, end);
}

// Real carracPositionStats formula (copied from static/js/chart-position-overlays.js — the
// painter/readout share this contract; the readout picks it up via window).
function carracPositionStats(priceRaw, entryRaw, slRaw) {
    var isLong = slRaw < entryRaw;
    var pct = isLong ? ((priceRaw - entryRaw) / entryRaw * 100)
                     : ((entryRaw - priceRaw) / entryRaw * 100);
    var risk = Math.abs(entryRaw - slRaw);
    var r = null, rLabel = '';
    if (entryRaw !== 0 && risk / Math.abs(entryRaw) >= 1e-9) {
        r = Math.abs(priceRaw - entryRaw) / risk;
        rLabel = (r >= 10 ? String(Math.round(r)) : r.toFixed(1).replace(/\.0$/, '')) + 'R';
    }
    return { pct: pct, r: r, rLabel: rLabel };
}

function loadReadoutPass(opts) {
    opts = opts || {};
    const showCalls = [];
    const hideCalls = [];
    const env = {
        klineSelectedOverlayId: opts.selectedOverlayId,
        _positionLevelDrag: null,
        _drawingToolActive: false,
        klineChart: { convertFromPixel: () => ({ value: opts.cursorPrice }) },
        _fmtObjPrice: (v) => String(v),
        console, Math, JSON, Object, Array, isFinite, parseFloat, parseInt,
    };
    env.window = env;
    env.window.carracPositionStats = carracPositionStats;
    env.window.carracFromChartSpace = (v) => v;
    env.window._showTpReadout = function(x, y, text) { showCalls.push({ x: x, y: y, text: text }); };
    env.window._hideTpReadout = function() { hideCalls.push(true); };

    const code = slice('// TP-zone hover readout', '// Hover/hit/cursor pass');
    new Script(code, { filename: 'tp-readout-pass-slice.js' }).runInContext(createContext(env));
    assert.equal(typeof env._tpReadoutPass, 'function', '_tpReadoutPass sliced');
    return { env: env, showCalls: showCalls, hideCalls: hideCalls };
}

// Shared long position: entry 100, SL 90, TP 130.
function makeHit(overrides) {
    return Object.assign({
        shapeType: 'position',
        interaction: 'body',
        level: 'body',
        overlayId: 'pos_1',
        drawing: {
            paneId: 'candle_pane',
            points: [{ value: 100 }, { value: 90 }, { value: 130 }],
            extendData: {},
        },
    }, overrides || {});
}

test('a) selected + unlocked, cursor in TP zone → shown with Alt hint', () => {
    const { env, showCalls } = loadReadoutPass({ selectedOverlayId: 'pos_1', cursorPrice: 110 });
    const hit = makeHit();
    const result = env._tpReadoutPass(50, 60, hit);
    assert.equal(result, true);
    assert.equal(showCalls.length, 1);
    assert.ok(showCalls[0].text.includes('+10.00%'), 'text: ' + showCalls[0].text);
    assert.ok(showCalls[0].text.includes('1R'), 'text: ' + showCalls[0].text);
    assert.ok(showCalls[0].text.includes('⌥click = TP'), 'text: ' + showCalls[0].text);
});

test('b) unselected but LOCKED, cursor in TP zone → shown WITHOUT Alt hint', () => {
    const { env, showCalls } = loadReadoutPass({ selectedOverlayId: 'some_other_overlay', cursorPrice: 110 });
    const hit = makeHit({ drawing: { paneId: 'candle_pane', points: [{ value: 100 }, { value: 90 }, { value: 130 }], extendData: { _locked: true } } });
    const result = env._tpReadoutPass(50, 60, hit);
    assert.equal(result, true);
    assert.equal(showCalls.length, 1);
    assert.ok(showCalls[0].text.includes('+10.00%'), 'text: ' + showCalls[0].text);
    assert.ok(showCalls[0].text.includes('1R'), 'text: ' + showCalls[0].text);
    assert.ok(!showCalls[0].text.includes('⌥click'), 'text should not include Alt hint: ' + showCalls[0].text);
});

test('c) unselected + unlocked → not shown, hide called', () => {
    const { env, showCalls, hideCalls } = loadReadoutPass({ selectedOverlayId: 'some_other_overlay', cursorPrice: 110 });
    const hit = makeHit();
    const result = env._tpReadoutPass(50, 60, hit);
    assert.equal(result, false);
    assert.equal(showCalls.length, 0);
    assert.ok(hideCalls.length >= 1, '_hideTpReadout should have been called');
});

test('d) selected, cursor below entry (SL zone) → not shown', () => {
    const { env, showCalls } = loadReadoutPass({ selectedOverlayId: 'pos_1', cursorPrice: 95 });
    const hit = makeHit();
    const result = env._tpReadoutPass(50, 60, hit);
    assert.equal(result, false);
    assert.equal(showCalls.length, 0);
});
