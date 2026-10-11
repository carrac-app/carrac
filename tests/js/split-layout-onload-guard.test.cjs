// "ReferenceError: initSplitLayout is not defined" at
// window.onload (Bugsink, 2026-09-09, release 0.226.16).
//
// split-layout.js and main.js are both loaded with `defer` and in that
// document order in templates/index.html, so under normal page loads
// initSplitLayout is defined long before window.onload fires. The observed
// crash is the same family as the kline-drawings.js / carrac-indicator-pack.js
// lazy-load failures (tests/js/lazy-loader-retry.test.cjs): a defer'd
// <script> tag can still fail to load over the network (blip, ad blocker, CDN
// hiccup) without blocking sibling <script> tags — so `initSplitLayout()`
// executing unguarded in window.onload threw and killed every statement
// after it in that handler: the details drawer, the row-click delegate, the
// onboarding retry loop, and the auto-reopen-chart / session-restore logic
// never ran either.
//
// This test slices the REAL guarded call site out of main.js (not a
// reimplementation) and runs it in a vm sandbox, once with initSplitLayout
// missing and once with it present.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const MAIN = fs.readFileSync(path.join(ROOT, 'static/js/main.js'), 'utf8');

const START_MARKER = '// Initialize split layout (desktop only)';
const END_MARKER = '// Details drawer';

function extractGuardBlock() {
    const start = MAIN.indexOf(START_MARKER);
    assert.notStrictEqual(start, -1, `marker "${START_MARKER}" not found in main.js`);
    const end = MAIN.indexOf(END_MARKER, start);
    assert.notStrictEqual(end, -1, `marker "${END_MARKER}" not found after the split-layout block`);
    return MAIN.slice(start, end);
}

function run(block, { withInitSplitLayout, withSentry } = {}) {
    const calls = { init: 0, carracLog: [], sentry: [] };
    const sandbox = {
        console,
        carracLog: function () { calls.carracLog.push(Array.from(arguments)); }
    };
    sandbox.window = sandbox;
    if (withInitSplitLayout) {
        sandbox.initSplitLayout = function () { calls.init++; };
    }
    if (withSentry) {
        sandbox.Sentry = { captureMessage: function () { calls.sentry.push(Array.from(arguments)); } };
    }
    vm.createContext(sandbox);
    // Mirror what really follows the block in window.onload: a marker
    // statement, to prove the chain doesn't die when the guard trips.
    vm.runInContext(block + '\nwindow.__afterMarker = true;', sandbox, { filename: 'onload-slice.js' });
    calls.afterRan = sandbox.__afterMarker === true;
    return calls;
}

test('calls initSplitLayout when it is defined, and nothing else fires', () => {
    const block = extractGuardBlock();
    const calls = run(block, { withInitSplitLayout: true, withSentry: true });
    assert.strictEqual(calls.init, 1);
    assert.strictEqual(calls.carracLog.length, 0);
    assert.strictEqual(calls.sentry.length, 0);
    assert.strictEqual(calls.afterRan, true);
});

test('missing initSplitLayout does not throw, and the rest of window.onload still runs', () => {
    const block = extractGuardBlock();
    const calls = run(block, { withInitSplitLayout: false, withSentry: false });
    assert.strictEqual(calls.afterRan, true, 'a statement after the guard must still execute');
});

test('missing initSplitLayout leaves a carracLog breadcrumb', () => {
    const block = extractGuardBlock();
    const calls = run(block, { withInitSplitLayout: false, withSentry: false });
    assert.strictEqual(calls.carracLog.length, 1);
    assert.strictEqual(calls.carracLog[0][0], 'assets');
    assert.match(calls.carracLog[0][1], /initSplitLayout/);
});

test('missing initSplitLayout beacons Sentry when the SDK is present', () => {
    const block = extractGuardBlock();
    const calls = run(block, { withInitSplitLayout: false, withSentry: true });
    assert.strictEqual(calls.sentry.length, 1);
    assert.match(calls.sentry[0][0], /initSplitLayout/);
});

test('missing initSplitLayout AND no Sentry SDK still does not throw', () => {
    const block = extractGuardBlock();
    assert.doesNotThrow(() => run(block, { withInitSplitLayout: false, withSentry: false }));
});
