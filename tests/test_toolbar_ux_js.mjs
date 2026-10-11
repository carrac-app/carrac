/**
 * JS unit tests for the toolbar-UX quick wins (chart app):
 *  - derived indicator-group pill state (_isGroupActive/_isGroupPartial/_refreshGroupPillStates)
 *  - favourite-tool icon map completeness (no '?' fallback for any named tool)
 *  - tooltip kbd-chip regex (real key combos only)
 *
 * Functions are sliced out of the chart app's source (the files listed in
 * tests/js/chart-app-sources.cjs) and evaluated in a VM
 * context with minimal mocks — no browser, no KLineChart.
 *
 * Run: node tests/test_toolbar_ux_js.mjs
 */

import { readFileSync } from 'fs';
import { createRequire } from 'module';
import { createContext, Script } from 'vm';
import { strict as assert } from 'assert';

let passed = 0;
let failed = 0;

function test(name, fn) {
    try {
        fn();
        passed++;
        process.stdout.write(`  \x1b[32m✓\x1b[0m ${name}\n`);
    } catch (e) {
        failed++;
        process.stdout.write(`  \x1b[31m✗\x1b[0m ${name}\n    ${e.message}\n`);
    }
}

const { chartAppSource } = createRequire(import.meta.url)('./js/chart-app-sources.cjs');
const src = chartAppSource();

function slice(startMarker, endMarker) {
    const start = src.indexOf(startMarker);
    assert.ok(start >= 0, `marker not found: ${startMarker}`);
    const end = src.indexOf(endMarker, start);
    assert.ok(end > start, `end marker not found: ${endMarker}`);
    return src.slice(start, end);
}

// ── Derived group state ─────────────────────────────────────────────

function makeGroupEnv() {
    const pills = {};
    function makePill(key) {
        const classes = new Set();
        return {
            classList: {
                toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); },
                contains: (c) => classes.has(c),
            },
            _classes: classes,
        };
    }
    const env = {
        klineActiveIndicators: {},
        klineIndicatorGroups: [],
        _pills: pills,
        _makePill: makePill,
        document: {
            querySelectorAll: (sel) => {
                const m = sel.match(/data-group-id="([^"]+)"/);
                if (!m) return [];
                const kind = sel.indexOf('mobile') >= 0 ? 'mobile' : 'desktop';
                const key = kind + ':' + m[1];
                if (!pills[key]) pills[key] = makePill(key);
                return [pills[key]];
            },
        },
    };
    const code = slice('function _isGroupActive', 'function renderFavouritePills');
    new Script(code).runInContext(createContext(env));
    return env;
}

test('group with all members on derives active', () => {
    const env = makeGroupEnv();
    env.klineActiveIndicators = { RSI: true, VOL: true };
    const g = { id: 'g1', indicators: ['RSI', 'VOL'] };
    assert.equal(env._isGroupActive(g), true);
    assert.equal(env._isGroupPartial(g), false);
});

test('group with one member off derives partial, not active', () => {
    const env = makeGroupEnv();
    env.klineActiveIndicators = { RSI: true, VOL: false };
    const g = { id: 'g1', indicators: ['RSI', 'VOL'] };
    assert.equal(env._isGroupActive(g), false);
    assert.equal(env._isGroupPartial(g), true);
});

test('group with all members off derives inactive and not partial', () => {
    const env = makeGroupEnv();
    env.klineActiveIndicators = { RSI: false, VOL: false };
    const g = { id: 'g1', indicators: ['RSI', 'VOL'] };
    assert.equal(env._isGroupActive(g), false);
    assert.equal(env._isGroupPartial(g), false);
});

test('empty group is never active or partial', () => {
    const env = makeGroupEnv();
    const g = { id: 'g1', indicators: [] };
    assert.equal(env._isGroupActive(g), false);
    assert.equal(env._isGroupPartial(g), false);
});

test('_refreshGroupPillStates updates desktop + mobile pill classes and persisted flag', () => {
    const env = makeGroupEnv();
    env.klineActiveIndicators = { RSI: true, VOL: true };
    const g = { id: 'g1', indicators: ['RSI', 'VOL'], active: false };  // stale persisted flag
    env.klineIndicatorGroups.push(g);
    env._refreshGroupPillStates();
    assert.equal(g.active, true, 'persisted flag reconciled');
    assert.ok(env._pills['desktop:g1']._classes.has('active'));
    assert.ok(env._pills['mobile:g1']._classes.has('active'));

    // Member turned off individually (the Indicators-menu path) → pill goes partial
    env.klineActiveIndicators.VOL = false;
    env._refreshGroupPillStates();
    assert.equal(g.active, false);
    assert.ok(!env._pills['desktop:g1']._classes.has('active'));
    assert.ok(env._pills['desktop:g1']._classes.has('partial'));
});

test('toggleKlineIndicator path calls _refreshGroupPillStates (wired, not just defined)', () => {
    const fnStart = src.indexOf('function toggleKlineIndicator(');
    const fnSlice = src.slice(fnStart, fnStart + 4000);
    assert.ok(fnSlice.indexOf('_refreshGroupPillStates') >= 0,
        'toggleKlineIndicator must refresh group pills');
});

test('toggleIndicatorGroup derives turningOn from live state, not the stored flag', () => {
    const fnStart = src.indexOf('function toggleIndicatorGroup(');
    const fnSlice = src.slice(fnStart, src.indexOf('\nfunction ', fnStart + 10));
    assert.ok(fnSlice.indexOf('var turningOn = !_isGroupActive(group)') >= 0);
    assert.ok(fnSlice.indexOf('_isGroupActive(g)') >= 0, 'protectedKeys must use derived state');
});

// ── Favourite-tool icon completeness ────────────────────────────────

test('every named tool has an icon (no ? fallback in the favourites bar)', () => {
    const env = {};
    const code = slice('var klineToolIcons = {', '\nfunction toggleFavouriteTool');
    new Script(code).runInContext(createContext(env));
    const missing = Object.keys(env.klineToolNames).filter(k => !env.klineToolIcons[k]);
    assert.deepEqual(missing, [], `tools without icons: ${missing.join(', ')}`);
});

// ── Tooltip kbd regex (extracted from source, not duplicated) ───────

test('tooltip regex chips real shortcuts and skips descriptive parens', () => {
    const tipSrc = readFileSync(new URL('../static/js/toolbar-tooltips.js', import.meta.url), 'utf8');
    const parseMatch = tipSrc.match(/var m = tip\.match\((\/[^;]+\/)\);/);
    const guardMatch = tipSrc.match(/if \(m && !(\/[^;]+\/)\.test\(m\[2\]\)\) m = null;/);
    assert.ok(parseMatch && guardMatch, 'tooltip regexes present in source');
    const parseRe = eval(parseMatch[1]);
    const guardRe = eval(guardMatch[1]);
    function parse(tip) {
        let m = tip.match(parseRe);
        if (m && !guardRe.test(m[2])) m = null;
        return m ? { label: m[1], kbd: m[2] } : null;
    }
    assert.deepEqual(parse('Parallel Channel (Alt+W)'), { label: 'Parallel Channel', kbd: 'Alt+W' });
    assert.deepEqual(parse('Clear All Drawings (hold Shift)'), { label: 'Clear All Drawings', kbd: 'hold Shift' });
    assert.equal(parse('Impulse (1-2-3-4-5)'), null, 'numeric parens must not chip');
    assert.equal(parse('Path — multi-point line'), null);
    assert.equal(parse('Magnet: snap to OHLC'), null);
});

// ── Shortcuts modal accuracy ────────────────────────────────────────

test('shortcuts modal documents S=scanner and N=news (matches the handlers)', () => {
    assert.ok(src.indexOf("sr('S', 'Scanner drawer')") >= 0, 'S row');
    assert.ok(src.indexOf("sr('N', 'News panel')") >= 0, 'N row');
    assert.ok(src.indexOf("sr('S', '(unbound)')") < 0, 'stale S row gone');
});

process.stdout.write(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
