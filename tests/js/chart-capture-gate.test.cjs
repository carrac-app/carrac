// tests/js/chart-capture-gate.test.cjs
// Unit tests for the pure capture-size decision core —
// share-modal.js's decideCaptureSize(). Raises the old 950,000-char gate to
// 5,500,000 (headroom under decode_chart_capture's 4 MiB decoded cap /
// carrac/plays/shares.py _MAX_CAPTURE_BYTES) and makes the decision testable
// in isolation from the DOM/toast wiring in chart-events.js.
const { test } = require('node:test');
const assert = require('node:assert');
const { decideCaptureSize } = require('../../static/js/share-modal.js');

test('a typical capture well under the cap is kept', () => {
    const d = decideCaptureSize(200000);
    assert.strictEqual(d.keep, true);
});

test('exactly at the cap is kept (inclusive boundary)', () => {
    const d = decideCaptureSize(5500000);
    assert.strictEqual(d.keep, true);
});

test('one char over the cap is dropped', () => {
    const d = decideCaptureSize(5500001);
    assert.strictEqual(d.keep, false);
    assert.strictEqual(d.reason, 'oversize');
});

test('the old 950,000-char gate no longer drops captures (gate raised)', () => {
    const d = decideCaptureSize(960000);
    assert.strictEqual(d.keep, true);
});

test('a very large busy-chart capture is dropped', () => {
    const d = decideCaptureSize(6000000);
    assert.strictEqual(d.keep, false);
    assert.strictEqual(d.reason, 'oversize');
});

test('zero length is dropped (nothing to share)', () => {
    const d = decideCaptureSize(0);
    assert.strictEqual(d.keep, false);
    assert.strictEqual(d.reason, 'empty');
});

test('negative length is dropped', () => {
    const d = decideCaptureSize(-1);
    assert.strictEqual(d.keep, false);
    assert.strictEqual(d.reason, 'empty');
});

test('non-number input is dropped, not thrown', () => {
    assert.strictEqual(decideCaptureSize(null).keep, false);
    assert.strictEqual(decideCaptureSize(undefined).keep, false);
    assert.strictEqual(decideCaptureSize('200000').keep, false);
    assert.strictEqual(decideCaptureSize(NaN).keep, false);
});
