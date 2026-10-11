/**
 * Share-render position painter parity — TP labels carry the same R multiple
 * as the live chart painter (kline-share-render.js keeps its own copy).
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createContext, Script } = require('node:vm');
const path = require('node:path');

const src = readFileSync(path.join(__dirname, '../../static/js/kline-share-render.js'), 'utf8');

function loadShareFigures() {
    const start = src.indexOf('function createPositionFigures(params) {');
    assert.ok(start >= 0, 'painter found');
    const end = src.indexOf("try { klinecharts.registerOverlay({ name: 'position'", start);
    assert.ok(end > start, 'registration found');
    const env = { console, Math, JSON, Object, Array };
    env.window = env;
    new Script(src.slice(start, end), { filename: 'share-painter-slice.js' }).runInContext(createContext(env));
    return env.createPositionFigures;
}

test('share painter: TP labels carry R multiple', () => {
    const fn = loadShareFigures();
    const figs = fn({
        coordinates: [{ x: 100, y: 300 }, { x: 100, y: 400 }, { x: 100, y: 100 }],
        bounding: { width: 900, height: 600 },
        overlay: {
            id: 'pos_1',
            points: [{ value: 100 }, { value: 90 }, { value: 130 }],
            extendData: { tps: [110] },
        },
    });
    const t = figs.filter(f => f.type === 'text').map(f => f.attrs.text);
    assert.ok(t.some(x => x.includes('TP1') && x.includes('· 1R')), 'itp: ' + JSON.stringify(t));
    assert.ok(t.some(x => x.includes('TP2') && x.includes('· 3R')), 'final: ' + JSON.stringify(t));
});
