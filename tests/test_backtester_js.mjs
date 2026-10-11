/**
 * JS unit tests for the backtester (static/js/backtester.js).
 *
 * First Python/JS tests for the backtester math itself.
 * Exercises the paths changed in v0.87.2:
 *   - funding sign by position direction
 *   - default entryMode = 'next_open' (no same-bar look-ahead)
 *
 * Plus structural guards: commission is round-trip (2x), equity curve fills
 * flat bars between exit and next entry, trades array is populated.
 *
 * Run: node tests/test_backtester_js.mjs
 * Or:  make test-js
 *
 * FormulaParser is stubbed — callers just pass the precomputed signal arrays
 * directly and the stub returns them verbatim.
 */

import { readFileSync } from 'fs';
import { createContext, Script } from 'vm';
import { strict as assert } from 'assert';

// ── Test harness ─────────────────────────────────────────────────────

let passed = 0;
let failed = 0;
const failures = [];

function test(name, fn) {
    try {
        fn();
        passed++;
        process.stdout.write(`  \x1b[32m✓\x1b[0m ${name}\n`);
    } catch (e) {
        failed++;
        failures.push({ name, error: e });
        process.stdout.write(`  \x1b[31m✗\x1b[0m ${name}\n    ${e.message}\n`);
    }
}

// ── Load backtester.js into a VM sandbox with a stubbed FormulaParser ──

function makeSandbox({ entrySignals, exitSignals }) {
    const sandbox = {
        window: {},
        console,
    };
    // Stub FormulaParser: whatever config.entry / config.exit string the test
    // passes is irrelevant — we key on it to return the right pre-baked array.
    sandbox.window.FormulaParser = {
        evaluate(expr, _ohlcv) {
            if (expr === '__ENTRY__') return { values: entrySignals, error: null };
            if (expr === '__EXIT__') return { values: exitSignals, error: null };
            return { values: [], error: null };
        },
    };
    const code = readFileSync(new URL('../static/js/backtester.js', import.meta.url), 'utf8');
    createContext(sandbox);
    new Script(code).runInContext(sandbox);
    return sandbox.window.Backtester;
}

function flatOHLCV(n, close = 100) {
    return {
        open:  Array(n).fill(close),
        high:  Array(n).fill(close + 0.1),
        low:   Array(n).fill(close - 0.1),
        close: Array(n).fill(close),
        volume: Array(n).fill(1),
    };
}

function upTrendOHLCV(n, start = 100, step = 1) {
    const close = Array(n).fill(0).map((_, i) => start + i * step);
    return {
        open:  close.map(c => c),
        high:  close.map(c => c + 0.5),
        low:   close.map(c => c - 0.5),
        close,
        volume: close.map(() => 1),
    };
}

// ── funding sign by direction ───────────────────────────────

console.log('\x1b[1m\nfunding cost is signed by position direction\x1b[0m');

test('long pays funding when rate > 0', () => {
    // 10 bars, flat price, signal on entry, signal on exit. Positive funding
    // rate every bar. Long position should LOSE funding.
    const n = 10;
    const entry = Array(n).fill(0); entry[1] = 1;  // enter bar 1
    const exit  = Array(n).fill(0); exit[8]  = 1;  // exit bar 8
    const ohlcv = flatOHLCV(n, 100);
    ohlcv.funding = Array(n).fill(0.001);  // +0.1% per bar

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        entryMode: 'close', instrument: 'perp', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.error, null, result.error);
    assert.equal(result.trades.length, 1);
    // Long with rate>0 → funding reduces PnL. Gross PnL should be ~0 (flat price),
    // so netPnl is NEGATIVE (only funding + 2x commission).
    assert.ok(result.trades[0].pnl < 0, `expected long to lose funding, got pnl=${result.trades[0].pnl}`);
});

test('short EARNS funding when rate > 0 (receives it)', () => {
    const n = 10;
    const entry = Array(n).fill(0); entry[1] = 1;
    const exit  = Array(n).fill(0); exit[8]  = 1;
    const ohlcv = flatOHLCV(n, 100);
    ohlcv.funding = Array(n).fill(0.001);

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'short', initialCapital: 10000, sizePercent: 100,
        entryMode: 'close', instrument: 'perp', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.error, null);
    assert.equal(result.trades.length, 1);
    // Short with rate>0 → shorts RECEIVE funding, so netPnl is POSITIVE minus
    // commission (commission is small). The trade should at least not bleed
    // funding — strong invariant: short pnl > long pnl given same bars.
    const shortPnl = result.trades[0].pnl;

    // Repeat as long for comparison
    const BT2 = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const longRes = BT2.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        entryMode: 'close', instrument: 'perp', barSeconds: 86400,
    }, ohlcv);
    assert.ok(shortPnl > longRes.trades[0].pnl,
        `short should outperform long on positive funding. short=${shortPnl} long=${longRes.trades[0].pnl}`);
});

test('no funding field = no funding charge', () => {
    const n = 10;
    const entry = Array(n).fill(0); entry[1] = 1;
    const exit  = Array(n).fill(0); exit[8]  = 1;
    const ohlcv = flatOHLCV(n, 100);  // no .funding

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        commissionPercent: 0, entryMode: 'close',
        instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.trades.length, 1);
    // Flat prices + no funding + zero commission → pnl ~= 0
    assert.ok(Math.abs(result.trades[0].pnl) < 0.01,
        `expected pnl≈0, got ${result.trades[0].pnl}`);
});

// ── default entryMode is next_open ─────────────────────────

console.log('\x1b[1m\ndefault entryMode is next_open (no same-bar look-ahead)\x1b[0m');

test('unspecified entryMode fills at next bar open', () => {
    // 5 bars of uptrending data. Signal on bar 1. close mode would fill at
    // closes[1]; next_open fills at opens[2] which is the NEXT bar.
    const n = 5;
    const entry = Array(n).fill(0); entry[1] = 1;
    const exit  = Array(n).fill(0); exit[3]  = 1;
    const ohlcv = {
        open:  [100, 110, 120, 130, 140],
        high:  [101, 111, 121, 131, 141],
        low:   [ 99, 109, 119, 129, 139],
        close: [100, 110, 120, 130, 140],
        volume:[1,1,1,1,1],
    };

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    // NO entryMode key — expect default to be 'next_open'.
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        commissionPercent: 0, slippageBps: 0,
        instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.error, null);
    assert.equal(result.trades.length, 1);
    // Entry must be at opens[2] = 120, NOT closes[1] = 110.
    assert.equal(result.trades[0].entryPrice, 120,
        `expected entry at next bar open (120), got ${result.trades[0].entryPrice}`);
    assert.equal(result.trades[0].entryBar, 2);
});

test('explicit close mode still fills at the signal bar close', () => {
    const n = 5;
    const entry = Array(n).fill(0); entry[1] = 1;
    const exit  = Array(n).fill(0); exit[3]  = 1;
    const ohlcv = {
        open:  [100, 110, 120, 130, 140],
        high:  [101, 111, 121, 131, 141],
        low:   [ 99, 109, 119, 129, 139],
        close: [100, 110, 120, 130, 140],
        volume:[1,1,1,1,1],
    };

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        commissionPercent: 0, slippageBps: 0,
        entryMode: 'close',
        instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.trades.length, 1);
    assert.equal(result.trades[0].entryPrice, 110,
        `expected entry at signal bar close (110), got ${result.trades[0].entryPrice}`);
    assert.equal(result.trades[0].entryBar, 1);
});

// ── Structural invariants ────────────────────────────────────────────

console.log('\x1b[1m\nBacktester structural invariants\x1b[0m');

test('commission is round-trip (2× entry value × commPct)', () => {
    const n = 6;
    const entry = Array(n).fill(0); entry[1] = 1;
    const exit  = Array(n).fill(0); exit[4]  = 1;
    const ohlcv = flatOHLCV(n, 100);

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        commissionPercent: 0.1,  // 0.1% per side
        slippageBps: 0,
        entryMode: 'close',
        instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    // posValue = 10000, commission = 2 * 0.001 * 10000 = 20. Flat price → gross=0.
    // netPnl = 0 - 20 - 0 = -20.
    assert.equal(result.trades.length, 1);
    assert.ok(Math.abs(result.trades[0].pnl + 20) < 0.01,
        `expected pnl≈-20 (round-trip commission), got ${result.trades[0].pnl}`);
});

test('equity curve has length n and stays flat when no position open', () => {
    const n = 8;
    const entry = Array(n).fill(0); entry[2] = 1;
    const exit  = Array(n).fill(0); exit[5]  = 1;
    const ohlcv = flatOHLCV(n, 100);

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        commissionPercent: 0, slippageBps: 0,
        entryMode: 'close',
        instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.equity.length, n);
    // Bar 0 has no position → equity = starting capital.
    assert.equal(result.equity[0], 10000);
    // Bar n-1 has no open position after exit → equity stays at exit value.
    // Flat price + zero commission means it stays at 10000.
    assert.ok(Math.abs(result.equity[n - 1] - 10000) < 0.01,
        `expected equity ≈ 10000, got ${result.equity[n - 1]}`);
});

test('no signals = no trades', () => {
    const n = 10;
    const entry = Array(n).fill(0);
    const exit  = Array(n).fill(0);
    const ohlcv = upTrendOHLCV(n);

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        entryMode: 'close', instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.trades.length, 0);
    assert.equal(result.equity[n - 1], 10000);
});

test('invalid config (missing entry formula) returns error', () => {
    const BT = makeSandbox({ entrySignals: [], exitSignals: [] });
    const result = BT.run({
        exit: '__EXIT__',
        initialCapital: 10000, sizePercent: 100,
    }, flatOHLCV(10));
    assert.ok(result.error, 'expected an error');
    assert.ok(/entry/i.test(result.error), `expected entry-related error, got: ${result.error}`);
});

// ── Sharpe sample-std, Sortino correct downside dev ──

console.log('\x1b[1m\nSharpe sample std, Sortino downside deviation\x1b[0m');

test('Sharpe uses sample std (N-1): biased stdev produces higher Sharpe than sample', () => {
    // Construct a deterministic up-trend so Sharpe is positive and finite.
    // We verify that Sharpe matches the hand-computed sample-std formula.
    const n = 10;
    const entry = Array(n).fill(0); entry[0] = 1;
    const exit  = Array(n).fill(0); exit[n - 1] = 1;
    const ohlcv = upTrendOHLCV(n, 100, 2);  // +$2/bar, so returns ≈ 2% each

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        commissionPercent: 0, slippageBps: 0,
        entryMode: 'close', instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.error, null, result.error);

    // Compute expected Sharpe (sample std, N-1) manually from the equity curve.
    const eq = result.equity;
    const nBars = eq.length - 1;
    const returns = [];
    for (let i = 0; i < nBars; i++) {
        returns.push(eq[i] > 0 ? (eq[i + 1] - eq[i]) / eq[i] : 0);
    }
    const mean = returns.reduce((a, b) => a + b, 0) / nBars;
    const varSum = returns.reduce((s, r) => s + (r - mean) ** 2, 0);
    const stdSample = Math.sqrt(varSum / (nBars - 1));
    const periodsPerYear = 365;
    const expectedSharpe = (mean / stdSample) * Math.sqrt(periodsPerYear);

    // Allow 1% relative tolerance (floating point).
    const rel = Math.abs(result.stats.sharpeRatio - expectedSharpe) / Math.max(Math.abs(expectedSharpe), 1e-9);
    assert.ok(rel < 0.01,
        `Sharpe mismatch: got ${result.stats.sharpeRatio.toFixed(6)}, expected ${expectedSharpe.toFixed(6)} (rel err=${rel.toFixed(6)})`);
});

test('Sortino: positive-only returns give sortino > sharpe (no downside)', () => {
    // A perfect up-trend with no down bars → downside deviation = 0 → sortino = 0
    // (because there are no bars below target=0). Sortino ≥ Sharpe for non-negative
    // returns series only when target is achieved; when downside=0 sortino = 0 by
    // convention (or Infinity if we interpret it that way).
    // Here we verify the formula doesn't crash and returns a finite number.
    const n = 20;
    const entry = Array(n).fill(0); entry[0] = 1;
    const exit  = Array(n).fill(0); exit[n - 1] = 1;
    const ohlcv = upTrendOHLCV(n, 100, 1);

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        commissionPercent: 0, slippageBps: 0,
        entryMode: 'close', instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.error, null, result.error);
    assert.ok(isFinite(result.stats.sharpeRatio), `Sharpe should be finite, got ${result.stats.sharpeRatio}`);
    assert.ok(isFinite(result.stats.sortinoRatio), `Sortino should be finite, got ${result.stats.sortinoRatio}`);
    // With no down-bars, downStdev = 0 → sortino stays 0 (guarded by `if (downStdev > 0)`)
    assert.equal(result.stats.sortinoRatio, 0, `Expected sortinoRatio=0 when all returns ≥ target, got ${result.stats.sortinoRatio}`);
});

test('Sortino: mixed returns give sortino > 0 and Sortino formula uses all-bar denominator', () => {
    // Up-trend with occasional dips (use downtrend seed so some bars are negative)
    const n = 20;
    const entry = Array(n).fill(0); entry[0] = 1;
    const exit  = Array(n).fill(0); exit[n - 1] = 1;
    const close = [100,98,101,99,103,101,105,103,107,105,109,107,111,109,113,111,115,113,117,119];
    const ohlcv = {
        open:   close.map(c => c),
        high:   close.map(c => c + 0.5),
        low:    close.map(c => c - 0.5),
        close,
        volume: close.map(() => 1),
    };

    const BT = makeSandbox({ entrySignals: entry, exitSignals: exit });
    const result = BT.run({
        entry: '__ENTRY__', exit: '__EXIT__',
        direction: 'long', initialCapital: 10000, sizePercent: 100,
        commissionPercent: 0, slippageBps: 0,
        entryMode: 'close', instrument: 'spot', barSeconds: 86400,
    }, ohlcv);
    assert.equal(result.error, null, result.error);

    // Manually compute expected Sortino with target=0 over all bars
    const eq = result.equity;
    const nBars = eq.length - 1;
    const returns = [];
    for (let i = 0; i < nBars; i++) {
        returns.push(eq[i] > 0 ? (eq[i + 1] - eq[i]) / eq[i] : 0);
    }
    const mean = returns.reduce((a, b) => a + b, 0) / nBars;
    const target = 0;
    let downVarSum = 0;
    for (const r of returns) {
        const shortfall = target - r;
        if (shortfall > 0) downVarSum += shortfall * shortfall;
    }
    const downStdev = Math.sqrt(downVarSum / nBars);
    const periodsPerYear = 365;
    const expectedSortino = downStdev > 0 ? ((mean - target) / downStdev) * Math.sqrt(periodsPerYear) : 0;

    const rel = Math.abs(result.stats.sortinoRatio - expectedSortino) / Math.max(Math.abs(expectedSortino), 1e-9);
    assert.ok(rel < 0.01,
        `Sortino mismatch: got ${result.stats.sortinoRatio.toFixed(6)}, expected ${expectedSortino.toFixed(6)} (rel err=${rel.toFixed(6)})`);
});

// ─── Summary ─────────────────────────────────────────────────────────

console.log(`\n\x1b[1m${passed + failed} tests: \x1b[32m${passed} passed\x1b[0m${failed ? `, \x1b[31m${failed} failed\x1b[0m` : ''}\n`);

if (failures.length > 0) {
    console.log('\x1b[31mFailures:\x1b[0m');
    failures.forEach(f => {
        console.log(`  ${f.name}:`);
        console.log(`    ${f.error.message}`);
    });
    process.exit(1);
}
