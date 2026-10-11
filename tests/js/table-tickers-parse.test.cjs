// tests/js/table-tickers-parse.test.cjs
// Unit tests for the pure Gate.io / Kraken / Bybit WS message parsers in
// table-tickers-ws.js (CoinCap replacement — CoinCap retired its keyless WS;
// CC is routed through Bybit).
// Run: node --test tests/js/table-tickers-parse.test.cjs
const { test } = require('node:test');
const assert = require('node:assert');
const { parseGateTickMessage, parseKrakenTickMessage, parseBybitTickMessage } = require('../../static/js/table-tickers-ws.js');

// ─── Gate.io spot.candlesticks ───────────────────────────────────────────

test('gate: candle update → {symbol, price}', function() {
    var msg = {
        channel: 'spot.candlesticks', event: 'update',
        result: { t: '1780825680', v: '0.006342', c: '0.6342', h: '0.6342', l: '0.6342', o: '0.6342', n: '1m_AKT_USDT' }
    };
    assert.deepStrictEqual(parseGateTickMessage(msg), { symbol: 'AKT', price: 0.6342 });
});

test('gate: DEEP pair parses to DEEP', function() {
    var msg = {
        channel: 'spot.candlesticks', event: 'update',
        result: { c: '0.1481', n: '1m_DEEP_USDT' }
    };
    assert.deepStrictEqual(parseGateTickMessage(msg), { symbol: 'DEEP', price: 0.1481 });
});

test('gate: PIPPIN pair parses to PIPPIN', function() {
    var msg = {
        channel: 'spot.candlesticks', event: 'update',
        result: { c: '0.02345', n: '1m_PIPPIN_USDT' }
    };
    assert.deepStrictEqual(parseGateTickMessage(msg), { symbol: 'PIPPIN', price: 0.02345 });
});

test('gate: subscribe ack → null', function() {
    var msg = {
        channel: 'spot.candlesticks', event: 'subscribe',
        result: { status: 'success' }
    };
    assert.strictEqual(parseGateTickMessage(msg), null);
});

test('gate: pong → null', function() {
    assert.strictEqual(parseGateTickMessage({ channel: 'spot.pong', event: null }), null);
});

test('gate: zero/garbage price → null', function() {
    var msg = { channel: 'spot.candlesticks', event: 'update', result: { c: '0', n: '1m_AKT_USDT' } };
    assert.strictEqual(parseGateTickMessage(msg), null);
    msg.result.c = 'nope';
    assert.strictEqual(parseGateTickMessage(msg), null);
});

test('gate: malformed n field → null', function() {
    var msg = { channel: 'spot.candlesticks', event: 'update', result: { c: '1.5', n: 'weird' } };
    assert.strictEqual(parseGateTickMessage(msg), null);
});

test('gate: null/undefined input → null', function() {
    assert.strictEqual(parseGateTickMessage(null), null);
    assert.strictEqual(parseGateTickMessage({}), null);
});

// ─── Kraken v2 ticker ────────────────────────────────────────────────────

test('kraken: ticker snapshot → {symbol, price}', function() {
    var msg = {
        channel: 'ticker', type: 'snapshot',
        data: [{ symbol: 'XMR/USD', bid: 306.58, ask: 307.05, last: 306.18, volume: 7643.45 }]
    };
    assert.deepStrictEqual(parseKrakenTickMessage(msg), { symbol: 'XMR', price: 306.18 });
});

test('kraken: ticker update → {symbol, price}', function() {
    var msg = { channel: 'ticker', type: 'update', data: [{ symbol: 'XMR/USD', last: 307.5 }] };
    assert.deepStrictEqual(parseKrakenTickMessage(msg), { symbol: 'XMR', price: 307.5 });
});

test('kraken: heartbeat → null', function() {
    assert.strictEqual(parseKrakenTickMessage({ channel: 'heartbeat' }), null);
});

test('kraken: status + subscribe ack → null', function() {
    assert.strictEqual(parseKrakenTickMessage({ channel: 'status', type: 'update', data: [{ system: 'online' }] }), null);
    assert.strictEqual(parseKrakenTickMessage({ method: 'subscribe', result: { channel: 'ticker' }, success: true }), null);
});

test('kraken: zero/negative/missing last → null', function() {
    assert.strictEqual(parseKrakenTickMessage({ channel: 'ticker', data: [{ symbol: 'XMR/USD', last: 0 }] }), null);
    assert.strictEqual(parseKrakenTickMessage({ channel: 'ticker', data: [{ symbol: 'XMR/USD', last: -1 }] }), null);
    assert.strictEqual(parseKrakenTickMessage({ channel: 'ticker', data: [{ symbol: 'XMR/USD' }] }), null);
});

test('kraken: empty data / null input → null', function() {
    assert.strictEqual(parseKrakenTickMessage({ channel: 'ticker', data: [] }), null);
    assert.strictEqual(parseKrakenTickMessage(null), null);
});

// ─── Bybit v5 public spot (CC only) ─────────────────────

test('bybit: kline.1 message → {symbol, price}', function() {
    var msg = {
        topic: 'kline.1.CCUSDT',
        data: [{ start: 1780825680000, open: '1.10', close: '1.23', high: '1.25', low: '1.09', volume: '1000', confirm: false }]
    };
    assert.deepStrictEqual(parseBybitTickMessage(msg), { symbol: 'CC', price: 1.23 });
});

test('bybit: pong reply → null', function() {
    assert.strictEqual(parseBybitTickMessage({ op: 'pong', success: true }), null);
});

test('bybit: subscribe ack → null', function() {
    assert.strictEqual(parseBybitTickMessage({ success: true, ret_msg: '', conn_id: 'abc', op: 'subscribe' }), null);
});

test('bybit: missing data → null', function() {
    assert.strictEqual(parseBybitTickMessage({ topic: 'kline.1.CCUSDT', data: [] }), null);
    assert.strictEqual(parseBybitTickMessage({ topic: 'kline.1.CCUSDT' }), null);
});

test('bybit: zero/garbage close → null', function() {
    var msg = { topic: 'kline.1.CCUSDT', data: [{ close: '0' }] };
    assert.strictEqual(parseBybitTickMessage(msg), null);
    msg.data[0].close = 'nope';
    assert.strictEqual(parseBybitTickMessage(msg), null);
});

test('bybit: null/undefined input → null', function() {
    assert.strictEqual(parseBybitTickMessage(null), null);
    assert.strictEqual(parseBybitTickMessage({}), null);
});
