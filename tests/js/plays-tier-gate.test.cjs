// tests/js/plays-tier-gate.test.cjs
// The frontend gate on Plays must not contradict the server.
//
// The server (TIER_LIMITS in carrac/auth/decorators.py) grants free users
// max_plays=3 and only 403s on the 4th. The frontend locked every free user
// outright, and the template rendered a "free" badge directly above
// that locked card. Three sources of truth, and the one a brand-new user saw
// was the wrong one.
const { test } = require('node:test');
const assert = require('node:assert');
const { _isPlaysLocked } = require('../../static/js/plays-panel.js');

function withGlobals(payments, tier, fn) {
    const prevPayments = global.window;
    global.window = { PAYMENTS_ENABLED: payments, _userTier: tier };
    try { return fn(); } finally { global.window = prevPayments; }
}

test('beta (payments off) + free user → NOT locked, matching the FREE badge', () => {
    assert.strictEqual(withGlobals(false, 'free', _isPlaysLocked), false);
});

test('beta (payments off) + anonymous visitor → NOT locked', () => {
    assert.strictEqual(withGlobals(false, undefined, _isPlaysLocked), false);
});

test('beta (payments off) + member → NOT locked', () => {
    assert.strictEqual(withGlobals(false, 'member', _isPlaysLocked), false);
});

test('payments live + member → not locked', () => {
    assert.strictEqual(withGlobals(true, 'member', _isPlaysLocked), false);
});

test('payments live + free → still gated (post-beta rule is a product decision)', () => {
    assert.strictEqual(withGlobals(true, 'free', _isPlaysLocked), true);
});
