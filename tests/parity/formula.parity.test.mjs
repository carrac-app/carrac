/**
 * JS-side parity test — loads the browser formula-parser.js in a minimal Node
 * harness (provides stub `window`, `TALib` dependencies) and verifies outputs
 * match the Python-generated golden fixture.
 *
 * Run: `node tests/parity/formula.parity.test.mjs`
 * Or add to any CI runner that can execute `node`.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..', '..');

function loadScript(relPath, sandbox) {
    const code = readFileSync(join(ROOT, relPath), 'utf8');
    vm.runInContext(code, sandbox, { filename: relPath });
}

function makeSandbox() {
    const sandbox = { console, setTimeout, clearTimeout };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    return sandbox;
}

function loadFormulaParser() {
    const sandbox = makeSandbox();
    loadScript('static/js/ta-lib.js', sandbox);
    loadScript('static/js/formula-parser.js', sandbox);
    return sandbox;
}

function approxEqual(a, b, tol = 1e-5) {
    if (a === null && b === null) return true;
    if (a === null || b === null) return false;
    if (typeof a !== 'number' || typeof b !== 'number') return false;
    if (Number.isNaN(a) && Number.isNaN(b)) return true;
    return Math.abs(a - b) < tol;
}

async function main() {
    const fixtures = JSON.parse(readFileSync(join(ROOT, 'tests/golden/formula/fixtures.json'), 'utf8'));
    const expected = JSON.parse(readFileSync(join(ROOT, 'tests/golden/formula/expected.json'), 'utf8'));

    const sandbox = loadFormulaParser();
    const FP = sandbox.window.FormulaParser;
    if (!FP || typeof FP.evaluate !== 'function') {
        console.error('FAIL: FormulaParser not loaded');
        process.exit(1);
    }
    if (FP.ENGINE_VERSION !== expected.engine_version) {
        console.error(`FAIL: JS engine v${FP.ENGINE_VERSION} vs golden v${expected.engine_version}`);
        process.exit(1);
    }

    const ohlcvByName = {};
    for (const f of fixtures.ohlcv_fixtures) ohlcvByName[f.name] = f;

    let pass = 0, fail = 0, skip = 0;
    // Expressions skipped in JS parity:
    //  - divergence_basic: DIVERGENCE has language-specific glue; dedicated unit tests cover it.
    const PY_ONLY = new Set(['divergence_basic']);

    for (const result of expected.results) {
        const label = `${result.fixture}/${result.expr_id}`;
        if (result.error) { skip++; continue; }
        if (PY_ONLY.has(result.expr_id)) { skip++; continue; }

        const ohlcv = ohlcvByName[result.fixture];
        const { values, error } = FP.evaluate(result.expr, ohlcv);
        if (error) {
            console.error(`FAIL ${label}: JS threw "${error}"`);
            fail++;
            continue;
        }
        if (values.length !== result.values.length) {
            console.error(`FAIL ${label}: length ${values.length} vs ${result.values.length}`);
            fail++;
            continue;
        }
        let mismatch = null;
        for (let i = 0; i < values.length; i++) {
            const a = Number.isFinite(values[i]) ? Math.round(values[i] * 1e6) / 1e6 : null;
            const e = result.values[i];
            if (!approxEqual(a, e)) {
                mismatch = { i, a, e };
                break;
            }
        }
        if (mismatch) {
            console.error(`FAIL ${label} @ bar ${mismatch.i}: JS ${mismatch.a} vs Py ${mismatch.e}`);
            fail++;
        } else {
            pass++;
        }
    }

    console.log(`Parity: ${pass} pass, ${fail} fail, ${skip} skipped`);
    if (fail > 0) process.exit(1);
}

main().catch((e) => { console.error(e); process.exit(1); });
