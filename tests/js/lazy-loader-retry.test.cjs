// tests/js/lazy-loader-retry.test.cjs
//
// Users lost the chart the same way (seen on Chrome/Windows and iPhone Safari): the two LAZILY
// loaded modules — kline-drawings.js and carrac-indicator-pack.js — fired the
// <script> element's onerror, then the chart init hit the bare
// `migrateKlineDrawingsToAssetKeys()` and threw a ReferenceError. Both files
// are Cloudflare edge-cached and served fine to the same users minutes
// earlier, so the failing copy came from a cache, not the origin.
//
// Two defects made a transient failure permanent for the whole page session:
//   1. ensureKLineDrawings() memoised the REJECTED promise, so every later
//      call returned the same rejection — no retry, ever.
//   2. the .catch fallbacks ("partial functionality beats hard failure") ran
//      the chart impl anyway, which called the missing global unguarded.
//
// These tests run the REAL loader functions sliced out of app-core.js in a vm
// context with a fake document, so the memo/retry logic is exercised as
// shipped rather than re-implemented here.
const { test } = require('node:test');
const assert = require('node:assert');
const vm = require('vm');
const { chartAppSource } = require('./chart-app-sources.cjs');

const APP = chartAppSource();

function extractFunction(src, name) {
    const start = src.indexOf(`function ${name}(`);
    assert.notStrictEqual(start, -1, `function ${name}( not found in the chart app`);
    let depth = 0, i = src.indexOf('{', start);
    for (; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}' && --depth === 0) break;
    }
    return src.slice(start, i + 1);
}

function makeContext() {
    const appended = [];
    const ctx = vm.createContext({
        document: {
            querySelector: () => null,
            createElement: () => ({}),
            head: { appendChild: (s) => appended.push(s) },
        },
        window: {},
        Date,
        Promise,
        encodeURIComponent,
        console: { error() {} },
    });
    const prelude = [
        'var _assetVersion; function _assetV() { return "0.0.0"; }',
        'var _klineDrawingsPromise = null;',
        extractFunction(APP, '_loadScript'),
        extractFunction(APP, '_loadModule'),
        extractFunction(APP, 'ensureKLineDrawings'),
    ].join('\n');
    vm.runInContext(prelude, ctx);
    return { ctx, appended };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

test('a failed lazy load is retried once with a cache-busting URL before it rejects', async () => {
    const { ctx, appended } = makeContext();
    let outcome = 'pending';
    vm.runInContext('ensureKLineDrawings().then(function(){ __ok = true; }, function(){ __ok = false; });',
        ctx);
    assert.strictEqual(appended.length, 1, 'first attempt appended');
    appended[0].onerror(new Error('cache served junk'));
    await tick();
    assert.strictEqual(appended.length, 2, 'one automatic retry appended');
    assert.notStrictEqual(appended[1].src, appended[0].src, 'retry must not reuse the failed URL');
    assert.match(appended[1].src, /kline-drawings\.js\?v=0\.0\.0&/, 'retry keeps the version and adds a buster');
    appended[1].onload();
    await tick();
    outcome = vm.runInContext('__ok', ctx);
    assert.strictEqual(outcome, true, 'a successful retry resolves the module promise');
});

test('after both attempts fail, the next ensureKLineDrawings() call tries again instead of replaying the rejection', async () => {
    const { ctx, appended } = makeContext();
    vm.runInContext('ensureKLineDrawings().catch(function(){ __failed = true; });', ctx);
    appended[0].onerror(new Error('1'));
    await tick();
    appended[1].onerror(new Error('2'));
    await tick();
    assert.strictEqual(vm.runInContext('__failed', ctx), true);
    assert.strictEqual(vm.runInContext('_klineDrawingsPromise', ctx), null,
        'the memo must be cleared on failure — a memoised rejection breaks the chart for the whole session');
    vm.runInContext('ensureKLineDrawings();', ctx);
    assert.strictEqual(appended.length, 3, 'a later call makes a fresh attempt');
});

test('a successful load is memoised (no duplicate script elements on repeat calls)', async () => {
    const { ctx, appended } = makeContext();
    vm.runInContext('ensureKLineDrawings(); ensureKLineDrawings();', ctx);
    assert.strictEqual(appended.length, 1);
    appended[0].onload();
    await tick();
    vm.runInContext('ensureKLineDrawings();', ctx);
    assert.strictEqual(appended.length, 1, 'resolved promise is reused');
});

test('chart init never calls the lazily-loaded drawing globals unguarded', () => {
    // The ReferenceError both users hit. The .catch fallbacks in
    // openAdvancedChartModal/loadKlineChart deliberately run the impl without
    // the modules, so every call into kline-drawings.js from that init path
    // must be typeof-guarded — same rule the SW comment already states for
    // klineContextMenu.
    for (const name of ['migrateKlineDrawingsToAssetKeys', 'loadKlineDrawings', 'loadDrawingsFromServer']) {
        const bare = new RegExp(`^\\s*${name}\\(`, 'm');
        assert.doesNotMatch(APP, bare, `${name}() is called bare at line start somewhere in the chart app`);
    }
});
