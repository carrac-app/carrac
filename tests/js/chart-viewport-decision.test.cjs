// tests/js/chart-viewport-decision.test.cjs
// Pure viewport-preservation decision for same-symbol chart reloads (boundary
// reload / recovery). Without this, every candle-close rebuild snaps the user
// back to the latest candle even while they study history. Snap only at the
// live edge; otherwise preserve their scroll + zoom.
const { test } = require('node:test');
const assert = require('node:assert');
const { chartViewportDecision } = require('../../static/js/chart-live-tick.js');

test('asset/TF switch always snaps (old viewport timestamps do not map)', () => {
    assert.strictEqual(
        chartViewportDecision({ isSwitch: true, lastRealIdx: 500, visibleRealTo: 100 }).mode,
        'snap');
});

test('missing viewport info → snap (safe default = current behavior)', () => {
    assert.strictEqual(chartViewportDecision({ isSwitch: false }).mode, 'snap');
    assert.strictEqual(
        chartViewportDecision({ isSwitch: false, lastRealIdx: 500 }).mode, 'snap');
    assert.strictEqual(chartViewportDecision(null).mode, 'snap');
});

test('at the live edge → snap (keep new candles visible)', () => {
    // rightmost visible real bar (realTo-1=499) is the latest real bar (500-ish)
    assert.strictEqual(
        chartViewportDecision({ isSwitch: false, lastRealIdx: 500, visibleRealTo: 501 }).mode,
        'snap');
    // phantom future bars visible past the last real bar — still the edge
    assert.strictEqual(
        chartViewportDecision({ isSwitch: false, lastRealIdx: 500, visibleRealTo: 505 }).mode,
        'snap');
});

test('within slack of the edge → snap (no flapping on a fresh candle append)', () => {
    // realTo-1 = 499, lastRealIdx = 500 → 1 bar back, inside SLACK(2)
    assert.strictEqual(
        chartViewportDecision({ isSwitch: false, lastRealIdx: 500, visibleRealTo: 500 }).mode,
        'snap');
    // exactly SLACK bars back → still snap (boundary inclusive)
    assert.strictEqual(
        chartViewportDecision({ isSwitch: false, lastRealIdx: 500, visibleRealTo: 499 }).mode,
        'snap');
});

test('scrolled meaningfully back into history → preserve', () => {
    // realTo-1 = 479, lastRealIdx = 500 → 21 bars back, well past SLACK
    assert.strictEqual(
        chartViewportDecision({ isSwitch: false, lastRealIdx: 500, visibleRealTo: 480 }).mode,
        'preserve');
    // just past the slack threshold (3 bars back)
    assert.strictEqual(
        chartViewportDecision({ isSwitch: false, lastRealIdx: 500, visibleRealTo: 498 }).mode,
        'preserve');
});
