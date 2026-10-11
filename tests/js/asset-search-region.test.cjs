/**
 * assetSearchCore.suffixRegion — exchange-suffix → market/currency hint for
 * the asset-search panel (e.g. "005930.KS" → Korea Exchange · KRW).
 *
 * Loads static/js/asset-search-core.js (IIFE attaching to window) in a VM.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createContext, Script } = require('node:vm');
const path = require('node:path');

function loadCore() {
    const env = { Math, JSON, Object, Array, String };
    env.window = env;
    const src = readFileSync(path.join(__dirname, '../../static/js/asset-search-core.js'), 'utf8');
    new Script(src, { filename: 'asset-search-core.js' }).runInContext(createContext(env));
    assert.ok(env.assetSearchCore, 'core attached to window');
    return env.assetSearchCore;
}

// VM-created objects have a different Object.prototype than the host realm,
// so deepStrictEqual rejects them — compare via JSON round-trip instead.
const plain = (o) => o == null ? o : JSON.parse(JSON.stringify(o));

test('known suffixes map to market + currency', () => {
    const core = loadCore();
    assert.deepEqual(plain(core.suffixRegion('005930.KS')), { market: 'Korea', currency: 'KRW' });
    assert.deepEqual(plain(core.suffixRegion('7203.T')), { market: 'Tokyo', currency: 'JPY' });
    assert.deepEqual(plain(core.suffixRegion('SAP.DE')), { market: 'Xetra', currency: 'EUR' });
    assert.deepEqual(plain(core.suffixRegion('BC94.L')), { market: 'London', currency: 'GBX' });
    assert.deepEqual(plain(core.suffixRegion('0005.HK')), { market: 'Hong Kong', currency: 'HKD' });
});

test('suffix-less and unknown-suffix symbols return null', () => {
    const core = loadCore();
    assert.equal(core.suffixRegion('AAPL'), null);
    assert.equal(core.suffixRegion('BTC'), null);
    assert.equal(core.suffixRegion('FOO.ZZZZ'), null);
});

test('ratio symbols (underscore) and lowercase input are handled', () => {
    const core = loadCore();
    assert.equal(core.suffixRegion('GOLD_QQQ'), null);
    assert.deepEqual(plain(core.suffixRegion('sap.de')), { market: 'Xetra', currency: 'EUR' });
});
