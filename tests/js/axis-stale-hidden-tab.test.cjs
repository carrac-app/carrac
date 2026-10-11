// "axis price stale" false alarm from a backgrounded tab.
//
// A hidden tab never repaints, so a stale axis label nobody is looking at is
// not a real staleness problem — and staleForS landing at exactly 61s is the tell:
// Chrome throttles a backgrounded tab's setInterval to ~once/minute, so the
// very first throttled tick after crossing the 60s persistence threshold in
// _carracNoteStale() (countdown-timer.js) fired the beacon while
// document.hidden was still true.
//
// These tests drive the REAL _carracNoteStale() (not a reimplementation) in
// a vm sandbox with a controllable clock and document.hidden, and spy on
// window.carracLog / Sentry.captureMessage to prove whether a beacon fires.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const SRC = fs.readFileSync(path.join(ROOT, 'static/js/countdown-timer.js'), 'utf8');

function harness() {
    let clock = Date.UTC(2026, 8, 14, 0, 1, 0); // 2026-09-14 00:01 UTC — the reported rollover
    let hidden = false;
    const carracLogCalls = [];
    const sentryCalls = [];

    const RealDate = Date;
    function FakeDate(...a) { return a.length ? new RealDate(...a) : new RealDate(clock); }
    FakeDate.now = () => clock;
    FakeDate.UTC = RealDate.UTC;
    FakeDate.prototype = RealDate.prototype;

    const document = { get hidden() { return hidden; } };

    const sandbox = {
        document, Date: FakeDate, Math, JSON, String, isFinite, console,
        allAssets: { BTC: { latest_price: 64950 } },
        _logScaleEnabled: false,
        carracLog: function () { carracLogCalls.push(Array.from(arguments)); },
        Sentry: { captureMessage: function () { sentryCalls.push(Array.from(arguments)); } }
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(SRC, sandbox, { filename: 'countdown-timer.js' });

    const barTs = clock - 86400000 - 5000; // ~1D-old bar, matches the reported case
    return {
        tick(ms) { clock += ms; },
        setHidden(v) { hidden = v; },
        note() {
            sandbox._carracNoteStale({ stale: true }, { timestamp: barTs, close: 64100 }, '1D', 'BTC', 'kline-candle-countdown');
        },
        carracLogCalls, sentryCalls
    };
}

test('does not beacon while the tab is hidden, even after the 60s window elapses', () => {
    const h = harness();
    h.note();                    // first stale observation — arms the clock, no beacon
    assert.strictEqual(h.sentryCalls.length, 0);

    h.setHidden(true);
    h.tick(61000);                // this is the exact reported shape: staleForS=61, hidden=true
    h.note();
    assert.strictEqual(h.sentryCalls.length, 0, 'must not beacon a stale axis nobody is looking at');
    assert.strictEqual(h.carracLogCalls.length, 0);
});

test('never beacons across an arbitrarily long hidden stretch', () => {
    const h = harness();
    h.note();
    h.setHidden(true);
    for (let i = 0; i < 10; i++) { h.tick(61000); h.note(); }
    assert.strictEqual(h.sentryCalls.length, 0);
});

test('requires the stale condition to persist a full 60s AFTER the tab becomes visible again', () => {
    const h = harness();
    h.note();                    // arm while visible
    h.setHidden(true);
    h.tick(61000);
    h.note();                    // first throttled tick while hidden: no beacon
    assert.strictEqual(h.sentryCalls.length, 0);

    h.setHidden(false);
    h.note();                    // becoming visible again restarts the clock, does not fire immediately
    assert.strictEqual(h.sentryCalls.length, 0);

    h.tick(30000);
    h.note();                    // still short of the fresh 60s window
    assert.strictEqual(h.sentryCalls.length, 0);

    h.tick(31000);
    h.note();                    // now 61s visible-and-stale — the real signal
    assert.strictEqual(h.sentryCalls.length, 1);
    assert.strictEqual(h.carracLogCalls.length, 1);
});

test('a genuinely stale bar that is always visible still beacons once (no regression)', () => {
    const h = harness();
    h.note();
    h.tick(61000);
    h.note();
    assert.strictEqual(h.sentryCalls.length, 1);
    assert.strictEqual(h.sentryCalls[0][1].tags.kind, 'axis_stale');
});
