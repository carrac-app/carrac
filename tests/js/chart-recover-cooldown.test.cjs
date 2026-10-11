// tests/js/chart-recover-cooldown.test.cjs
// Pure cooldown gate for the shared chart-recovery entry point (round 9 W1).
const { test } = require('node:test');
const assert = require('node:assert');
const { chartRecoverGate } = require('../../static/js/chart-live-tick.js');

test('first call allowed, stamps 30s cooldown', () => {
    const r = chartRecoverGate(1_000_000, 0, false);
    assert.strictEqual(r.allowed, true);
    assert.strictEqual(r.until, 1_030_000);
});
test('second call within 30s blocked', () => {
    const r = chartRecoverGate(1_000_000, 1_025_000, false);
    assert.strictEqual(r.allowed, false);
    assert.strictEqual(r.until, 1_025_000);
});
test('call after cooldown expiry allowed', () => {
    const r = chartRecoverGate(1_000_000, 999_999, false);
    assert.strictEqual(r.allowed, true);
});
test('force bypasses active cooldown', () => {
    const r = chartRecoverGate(1_000_000, 1_025_000, true);
    assert.strictEqual(r.allowed, true);
});
test('force still stamps a fresh cooldown', () => {
    const r = chartRecoverGate(1_000_000, 1_025_000, true);
    assert.strictEqual(r.until, 1_030_000);
});
test('zero/absent cooldownUntil treated as no cooldown', () => {
    assert.strictEqual(chartRecoverGate(5, undefined, false).allowed, true);
});
