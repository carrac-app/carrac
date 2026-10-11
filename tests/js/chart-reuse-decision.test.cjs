// tests/js/chart-reuse-decision.test.cjs
// Pure gate: on a fetch-path reload, may we REUSE the existing chart (light
// in-place fast-swap) instead of disposing+recreating it? Only when it's the
// SAME symbol+timeframe already on screen. Switches and recovery (which null
// _lastRenderedSymbol first) must fall through to a full rebuild.
const { test } = require('node:test');
const assert = require('node:assert');
const { shouldReuseChart } = require('../../static/js/chart-live-tick.js');

test('same symbol + same timeframe + live chart → reuse', () => {
    assert.strictEqual(shouldReuseChart({
        hasChart: true, symbol: 'BTC', timeframe: '1m',
        lastRenderedSymbol: 'BTC', lastRenderedTimeframe: '1m'
    }), true);
});

test('different symbol → full rebuild', () => {
    assert.strictEqual(shouldReuseChart({
        hasChart: true, symbol: 'ETH', timeframe: '1m',
        lastRenderedSymbol: 'BTC', lastRenderedTimeframe: '1m'
    }), false);
});

test('different timeframe → full rebuild', () => {
    assert.strictEqual(shouldReuseChart({
        hasChart: true, symbol: 'BTC', timeframe: '5m',
        lastRenderedSymbol: 'BTC', lastRenderedTimeframe: '1m'
    }), false);
});

test('no live chart yet → full rebuild (must create it)', () => {
    assert.strictEqual(shouldReuseChart({
        hasChart: false, symbol: 'BTC', timeframe: '1m',
        lastRenderedSymbol: 'BTC', lastRenderedTimeframe: '1m'
    }), false);
});

test('recovery nulled _lastRenderedSymbol → full rebuild (clean teardown)', () => {
    // _recoverChart / _staleChartRecover null _lastRenderedSymbol before reloading,
    // so a recovery reload must NOT take the reuse path.
    assert.strictEqual(shouldReuseChart({
        hasChart: true, symbol: 'BTC', timeframe: '1m',
        lastRenderedSymbol: null, lastRenderedTimeframe: null
    }), false);
});

test('missing/empty inputs → full rebuild (safe default)', () => {
    assert.strictEqual(shouldReuseChart({}), false);
    assert.strictEqual(shouldReuseChart(null), false);
    // empty-string lastRendered (pre-first-render) must not match an empty symbol
    assert.strictEqual(shouldReuseChart({
        hasChart: true, symbol: '', timeframe: '', lastRenderedSymbol: '', lastRenderedTimeframe: ''
    }), false);
});
