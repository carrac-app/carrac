// tests/js/conn-store.test.cjs
// Unit tests for CarracConn aggregate derivation (resilience round 9 W3).
// Run: node --test tests/js/conn-store.test.cjs
const { test } = require('node:test');
const assert = require('node:assert');
const { deriveAggregate } = require('../../static/js/conn-store.js');

// Case 1: all six 'connected', onLine true → 'live'
test('all six connected + onLine → live', function() {
    var states = { binance: 'connected', finnhub: 'connected', hyperliquid: 'connected', kraken: 'connected', gate: 'connected', ccxt: 'connected' };
    assert.strictEqual(deriveAggregate(states, true), 'live');
});

// Case 2: binance 'reconnecting', rest 'connected' → 'reconnecting'
test('binance reconnecting, rest connected → reconnecting', function() {
    var states = { binance: 'reconnecting', finnhub: 'connected', hyperliquid: 'connected', kraken: 'connected', gate: 'connected', ccxt: 'connected' };
    assert.strictEqual(deriveAggregate(states, true), 'reconnecting');
});

// Case 3: binance 'down' → 'offline'
test('binance down → offline', function() {
    var states = { binance: 'down', finnhub: 'connected', hyperliquid: 'connected', kraken: 'connected', gate: 'connected', ccxt: 'connected' };
    assert.strictEqual(deriveAggregate(states, true), 'offline');
});

// Case 4: finnhub 'reconnecting', rest 'connected' → 'degraded'
test('finnhub reconnecting, rest connected → degraded', function() {
    var states = { binance: 'connected', finnhub: 'reconnecting', hyperliquid: 'connected', kraken: 'connected', gate: 'connected', ccxt: 'connected' };
    assert.strictEqual(deriveAggregate(states, true), 'degraded');
});

// Case 5: kraken 'down' alone → 'degraded' (not offline)
test('kraken down alone → degraded not offline', function() {
    var states = { binance: 'connected', finnhub: 'connected', hyperliquid: 'connected', kraken: 'down', gate: 'connected', ccxt: 'connected' };
    assert.strictEqual(deriveAggregate(states, true), 'degraded');
});

// Case 6: onLine false overrides all-connected → 'offline'
test('onLine false overrides all-connected → offline', function() {
    var states = { binance: 'connected', finnhub: 'connected', hyperliquid: 'connected', kraken: 'connected', gate: 'connected', ccxt: 'connected' };
    assert.strictEqual(deriveAggregate(states, false), 'offline');
});

// Case 7: all six 'init' → 'init'
test('all six init → init', function() {
    var states = { binance: 'init', finnhub: 'init', hyperliquid: 'init', kraken: 'init', gate: 'init', ccxt: 'init' };
    assert.strictEqual(deriveAggregate(states, true), 'init');
});

// Case 8: ccxt 'init', other four 'connected' → 'live' (chart-only provider neutral)
test('ccxt init, other five connected → live', function() {
    var states = { binance: 'connected', finnhub: 'connected', hyperliquid: 'connected', kraken: 'connected', gate: 'connected', ccxt: 'init' };
    assert.strictEqual(deriveAggregate(states, true), 'live');
});
