// tests/js/chart-tick-decision.test.cjs
// Unit tests for the pure decision core of chart-live-tick.js (spinner saga round 8).
// Run: node --test tests/js/
//
// NOTE: applyChartTick (the browser wrapper) has an early-return fast-path added
// after the mismatch-clear block that skips the getDataList scan and ctx allocation
// for any off-symbol tick while state is healthy. It is deliberately untested here:
// the fast-path is wrapper-level DOM/global code that cannot be exercised without a
// full browser harness. The decision-core behaviour it bypasses is covered by the
// existing "tick for a different symbol is ignored" test above.
const { test } = require('node:test');
const assert = require('node:assert');
const { chartTickDecision } = require('../../static/js/chart-live-tick.js');

// Healthy baseline: BTC rendered, BTC intended, BTC tick, mid-candle, no throttle.
function ctx(overrides) {
    return Object.assign({
        intended: 'BTC',
        rendered: 'BTC',
        tick: 'BTC',
        panning: false,
        now: 1_000_000,
        tf: '1D',
        lastUpdateAt: 0,
        lastBarTs: 1_000_000 - 60_000,   // bar opened 1 min ago
        barMs: 86_400_000,                // 1D bar — boundary NOT crossed
        mismatchSince: null,
        boundary: null,
        recoverAt: 0
    }, overrides);
}

test('healthy tick updates candle in place', () => {
    assert.strictEqual(chartTickDecision(ctx({})).type, 'inplace');
});

test('throttle suppresses ticks within 250ms', () => {
    const a = chartTickDecision(ctx({ lastUpdateAt: 1_000_000 - 100 }));
    assert.strictEqual(a.type, 'ignore');
});

test('tick for a different symbol is ignored', () => {
    assert.strictEqual(chartTickDecision(ctx({ tick: 'ETH' })).type, 'ignore');
});

test('panning suppresses updates', () => {
    assert.strictEqual(chartTickDecision(ctx({ panning: true })).type, 'ignore');
});

test('no candle data is ignored', () => {
    assert.strictEqual(chartTickDecision(ctx({ lastBarTs: null })).type, 'ignore');
});

test('empty rendered symbol (pre-first-render) still updates in place', () => {
    // matches the old guards: _lastRenderedSymbol === '' passes
    assert.strictEqual(chartTickDecision(ctx({ rendered: '' })).type, 'inplace');
});

test('boundary crossed → first reload attempt, not a retry', () => {
    const a = chartTickDecision(ctx({ lastBarTs: 1_000_000 - 90_000_000 })); // bar long over
    assert.strictEqual(a.type, 'boundary-reload');
    assert.strictEqual(a.retry, false);
    assert.strictEqual(a.symbol, 'BTC');
    assert.strictEqual(a.key, 'BTC|1D|' + (1_000_000 - 90_000_000));
});

test('boundary reload NOT retried within 30s cooldown', () => {
    const lastBarTs = 1_000_000 - 90_000_000;
    const key = 'BTC|1D|' + lastBarTs;
    const a = chartTickDecision(ctx({
        lastBarTs, boundary: { key, at: 1_000_000 - 10_000 }
    }));
    assert.strictEqual(a.type, 'ignore');
});

test('boundary reload RETRIED after 30s cooldown (fixes one-shot _wsBoundaryReloadKey)', () => {
    const lastBarTs = 1_000_000 - 90_000_000;
    const key = 'BTC|1D|' + lastBarTs;
    const a = chartTickDecision(ctx({
        lastBarTs, boundary: { key, at: 1_000_000 - 31_000 }
    }));
    assert.strictEqual(a.type, 'boundary-reload');
    assert.strictEqual(a.retry, true);
});

test('symbol mismatch starts the observation clock', () => {
    const a = chartTickDecision(ctx({ intended: 'ADA', rendered: 'BTC' }));
    assert.strictEqual(a.type, 'observe-mismatch');
});

test('symbol mismatch younger than 12s grace does NOT recover (normal switch in flight)', () => {
    const a = chartTickDecision(ctx({
        intended: 'ADA', rendered: 'BTC', mismatchSince: 1_000_000 - 5_000
    }));
    assert.strictEqual(a.type, 'ignore');
});

test('persistent mismatch ≥12s recovers the INTENDED symbol (fixes failed-switch deadlock)', () => {
    const a = chartTickDecision(ctx({
        intended: 'ADA', rendered: 'BTC', mismatchSince: 1_000_000 - 13_000
    }));
    assert.strictEqual(a.type, 'deadlock-recover');
    assert.strictEqual(a.symbol, 'ADA');
});

test('deadlock recovery respects the 30s cooldown', () => {
    const a = chartTickDecision(ctx({
        intended: 'ADA', rendered: 'BTC',
        mismatchSince: 1_000_000 - 60_000,
        recoverAt: 1_000_000 - 10_000
    }));
    assert.strictEqual(a.type, 'ignore');
});

test('deadlock check fires on ticks of ANY symbol (tick stream is the clock)', () => {
    const a = chartTickDecision(ctx({
        intended: 'ADA', rendered: 'BTC', tick: 'SOL',
        mismatchSince: 1_000_000 - 13_000
    }));
    assert.strictEqual(a.type, 'deadlock-recover');
    assert.strictEqual(a.symbol, 'ADA');
});

test('null intended symbol (no chart open) is ignored', () => {
    assert.strictEqual(chartTickDecision(ctx({ intended: null, rendered: '' })).type, 'ignore');
});

test('unknown timeframe (barMs=0) never boundary-reloads — falls through to inplace', () => {
    const a = chartTickDecision(ctx({ barMs: 0, lastBarTs: 1_000_000 - 90_000_000 }));
    assert.strictEqual(a.type, 'inplace');
});

// --- Companion-pane contexts (split view fan-out, _applyCompanionTicks) ---
// Companion panes reuse chartTickDecision with intended === rendered === the
// pane's own symbol and per-pane throttle state. These pin that contract.

function companionCtx(overrides) {
    return Object.assign({
        intended: 'ETH',                 // pane symbol
        rendered: 'ETH',                 // companions have no switch states
        tick: 'ETH',
        panning: false,
        now: 1_000_000,
        tf: '4h',
        lastUpdateAt: 0,                 // p._lastTickAt
        lastBarTs: 1_000_000 - 60_000,
        barMs: 14_400_000,               // 4h bar — boundary NOT crossed
        mismatchSince: null,
        boundary: null,
        recoverAt: 0
    }, overrides);
}

test('companion: healthy tick updates in place', () => {
    assert.strictEqual(chartTickDecision(companionCtx({})).type, 'inplace');
});

test('companion: per-pane throttle suppresses ticks within 250ms', () => {
    const a = chartTickDecision(companionCtx({ lastUpdateAt: 1_000_000 - 100 }));
    assert.strictEqual(a.type, 'ignore');
});

test('companion: boundary crossed → boundary-reload (fan-out ignores it; countdown loop owns the reload)', () => {
    const a = chartTickDecision(companionCtx({ lastBarTs: 1_000_000 - 20_000_000 }));
    assert.strictEqual(a.type, 'boundary-reload');
    assert.strictEqual(a.symbol, 'ETH');
});

test('companion: intended===rendered makes mismatch/deadlock cases unreachable', () => {
    // Even with an ancient mismatchSince, the healthy-symbol path wins.
    const a = chartTickDecision(companionCtx({ mismatchSince: 1_000_000 - 999_000 }));
    assert.strictEqual(a.type, 'inplace');
});
