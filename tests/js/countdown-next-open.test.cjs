// tests/js/countdown-next-open.test.cjs
//
// Release 0.226.4 made every bar carry its OPEN timestamp. Before that,
// weekly/monthly bars were labelled by their END, so `lastTimestamp` could lie
// in the future and getNextCandleOpenMs() carried two
// `if (lastTimestamp > Date.now()) return lastTimestamp;` workarounds that
// echoed the input back. Those branches are dead now, and worse than dead:
// echoing a future label would make the countdown show the wrong close time
// if a mislabelled bar ever slipped through again. Pin the open-labelled
// semantics and prove the echo is gone.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
const SRC = fs.readFileSync(path.join(ROOT, 'static/js/countdown-timer.js'), 'utf8');

const NOW = Date.UTC(2026, 8, 7, 12, 30, 0);    // 2026-09-07 12:30 UTC, a Monday

function load() {
    const FakeDate = function (...a) { return a.length ? new Date(...a) : new Date(NOW); };
    FakeDate.now = () => NOW;
    FakeDate.UTC = Date.UTC;
    const ctx = vm.createContext({ Date: FakeDate, window: {}, document: { getElementById: () => null } });
    vm.runInContext(SRC, ctx);
    return ctx;
}

test('1h: next open is the open of the current bar plus one hour', () => {
    const ctx = load();
    const open = Date.UTC(2026, 8, 7, 12, 0, 0);
    assert.strictEqual(ctx.getNextCandleOpenMs(open, '1h'), open + 3600000);
});

test('1W: a week bar labelled by its Monday open counts down to next Monday', () => {
    const ctx = load();
    const monday = Date.UTC(2026, 8, 7, 0, 0, 0);
    assert.strictEqual(ctx.getNextCandleOpenMs(monday, '1W'), monday + 7 * 86400000);
});

test('1M: a month bar labelled by the 1st counts down to the 1st of next month', () => {
    const ctx = load();
    const first = Date.UTC(2026, 8, 1);
    assert.strictEqual(ctx.getNextCandleOpenMs(first, '1M'), Date.UTC(2026, 9, 1));
});

test('a stale open is stepped forward past now, not echoed', () => {
    const ctx = load();
    const oldOpen = Date.UTC(2026, 8, 7, 9, 0, 0);     // three bars ago on 1h
    assert.strictEqual(ctx.getNextCandleOpenMs(oldOpen, '1h'), Date.UTC(2026, 8, 7, 13, 0, 0));
});

test('a future label is never echoed back (the end-labelled workaround is gone)', () => {
    const ctx = load();
    const future = NOW + 15 * 60000;                     // an end-labelled 1h bar would look like this
    assert.notStrictEqual(ctx.getNextCandleOpenMs(future, '1h'), future);
    assert.strictEqual(ctx.getNextCandleOpenMs(future, '1h'), future + 3600000);
    const futureMonth = Date.UTC(2026, 9, 1);
    assert.notStrictEqual(ctx.getNextCandleOpenMs(futureMonth, '1M'), futureMonth);
    assert.strictEqual(ctx.getNextCandleOpenMs(futureMonth, '1M'), Date.UTC(2026, 10, 1));
});
