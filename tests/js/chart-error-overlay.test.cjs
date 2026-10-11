// tests/js/chart-error-overlay.test.cjs
// Pure decision model for the chart error overlay. The old
// .catch branded a transient data 404 as "Error: HTTP 404" with dismiss-only
// advice — wrong for the rate-limit window, which heals in ~2min
// and deserves a retry, and jargon for everyone else.
const { test } = require('node:test');
const assert = require('node:assert');
const { chartErrorOverlayModel } = require('../../static/js/chart-live-tick.js');

test('timeout offers retry and says the server may be busy', () => {
    const m = chartErrorOverlayModel({ timedOut: true, symbol: 'BTC' });
    assert.strictEqual(m.retry, true);
    assert.match(m.headline, /too long/i);
    assert.match(m.sub, /retry/i);
    assert.ok(m.logMsg);
});

test('AbortError is treated as a timeout even without the flag', () => {
    const m = chartErrorOverlayModel({ name: 'AbortError', symbol: 'BTC' });
    assert.strictEqual(m.retry, true);
    assert.match(m.headline, /too long/i);
});

test('data 404 names the symbol, offers retry, and drops the HTTP jargon', () => {
    const m = chartErrorOverlayModel({ status: 404, message: 'HTTP 404', symbol: 'BTC-USD' });
    assert.strictEqual(m.retry, true);
    assert.match(m.headline, /BTC-USD/);
    assert.doesNotMatch(m.headline, /HTTP/);
    assert.doesNotMatch(m.sub, /HTTP/);
    assert.match(m.logMsg, /404/);
});

test('data 404 without a symbol still reads as a sentence', () => {
    const m = chartErrorOverlayModel({ status: 404, message: 'HTTP 404' });
    assert.strictEqual(m.retry, true);
    assert.match(m.headline, /chart data/i);
    assert.doesNotMatch(m.headline, /undefined|null/);
});

test('5xx offers retry in plain language', () => {
    const m = chartErrorOverlayModel({ status: 502, message: 'HTTP 502', symbol: 'ETH' });
    assert.strictEqual(m.retry, true);
    assert.doesNotMatch(m.headline, /HTTP/);
    assert.match(m.logMsg, /502/);
});

test('a plain JS error stays dismiss-only and keeps the message for diagnosis', () => {
    const m = chartErrorOverlayModel({ message: 'x is not a function' });
    assert.strictEqual(m.retry, false);
    assert.match(m.sub, /x is not a function/);
});

test('no argument at all does not throw', () => {
    const m = chartErrorOverlayModel();
    assert.strictEqual(typeof m.headline, 'string');
    assert.strictEqual(typeof m.retry, 'boolean');
});
