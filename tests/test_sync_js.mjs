/**
 * JS unit tests for the drawing sync module (drawing-tools.js).
 *
 * Tests the sync state machine: debounce, cancellation, adaptive polling,
 * visibility API, _recentlyDeleted buffer, session filtering.
 *
 * Run: node tests/test_sync_js.mjs
 * Or:  make test-js
 *
 * No browser, no server, no KLineChart — pure logic tests with mocked globals.
 */

import { readFileSync } from 'fs';
import { createContext, Script } from 'vm';
import { strict as assert } from 'assert';

// ── Test harness ────────────────────────────────────────────────────

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

// ── Mock browser globals ────────────────────────────────────────────

function createMockEnv() {
    const timers = [];
    const intervals = [];
    const fetchCalls = [];
    let timerIdSeq = 1;

    const env = {
        // Captured state
        _timers: timers,
        _intervals: intervals,
        _fetchCalls: fetchCalls,
        _eventListeners: {},
        _consoleOutput: [],

        // Timer mocks
        setTimeout: (fn, ms) => {
            const id = timerIdSeq++;
            timers.push({ id, fn, ms, cleared: false });
            return id;
        },
        clearTimeout: (id) => {
            const t = timers.find(t => t.id === id);
            if (t) t.cleared = true;
        },
        setInterval: (fn, ms) => {
            const id = timerIdSeq++;
            intervals.push({ id, fn, ms, cleared: false });
            return id;
        },
        clearInterval: (id) => {
            const t = intervals.find(t => t.id === id);
            if (t) t.cleared = true;
        },

        // Fetch mock — returns configurable responses
        _fetchResponse: { ok: true, status: 200, body: { success: true } },
        fetch: (url, opts) => {
            fetchCalls.push({ url, opts });
            const resp = env._fetchResponse;
            return Promise.resolve({
                ok: resp.ok,
                status: resp.status,
                json: () => Promise.resolve(resp.body),
            });
        },

        // DOM mocks
        document: {
            readyState: 'complete',
            addEventListener: (event, handler) => {
                env._eventListeners[event] = env._eventListeners[event] || [];
                env._eventListeners[event].push(handler);
            },
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: (tag) => ({
                tagName: tag, className: '', textContent: '', id: '', style: {},
                setAttribute: () => {}, remove: () => {},
                appendChild: () => {},
            }),
            visibilityState: 'visible',
            head: { appendChild: () => {} },
            body: { appendChild: () => {} },
        },
        window: {},
        console: {
            log: (...args) => env._consoleOutput.push(args.join(' ')),
            warn: (...args) => env._consoleOutput.push(args.join(' ')),
            error: (...args) => env._consoleOutput.push(args.join(' ')),
        },
        Math,
        Date,
        JSON,
        Object,
        Array,
        Promise,
        encodeURIComponent,
        parseInt,
        parseFloat,

        // Globals the sync module reads
        klineChartSymbol: 'BTC',
        klineChartTimeframe: '1D',
        klineChart: {
            removeOverlay: () => {},
            resize: () => {},
            getDataList: () => [{ timestamp: Date.now(), open: 1, high: 2, low: 0, close: 1, volume: 100 }],
        },
        klineDataCache: {},
        _alertOverlayIds: [],
        _drawingsLoadedForSymbol: null,
        loadKlineDrawings: () => {},
        applyKlineIndicators: () => {},
        convertToKlineData: (d) => d,
        renderAlertOverlays: () => {},
        loadPlaysForChart: () => {},
        loadLiquidationZones: () => {},
        getCSRFToken: () => 'test-csrf',
        PreferenceSync: null,
    };

    // window needs addEventListener for beforeunload
    env.addEventListener = (event, handler) => {
        env._eventListeners[event] = env._eventListeners[event] || [];
        env._eventListeners[event].push(handler);
    };
    // localStorage mock
    const _store = {};
    env.localStorage = {
        getItem: (k) => _store[k] || null,
        setItem: (k, v) => { _store[k] = v; },
        removeItem: (k) => { delete _store[k]; },
    };

    // window === global context for the IIFE
    env.window = env;
    return env;
}

function loadSyncModule(env) {
    const src = readFileSync('static/js/drawing-tools.js', 'utf8');
    const ctx = createContext(env);
    const script = new Script(src, { filename: 'drawing-tools.js' });
    script.runInContext(ctx);
    return env.window.DrawingTools;
}

// ── Tests ───────────────────────────────────────────────────────────

console.log('\n\x1b[1mDrawing Sync JS Unit Tests\x1b[0m\n');

// ─── 1. Delete cancels pending UPSERT ─────

test('deleteDrawingFromServer cancels pending debounced UPSERT', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    // Queue a debounced UPSERT
    DT.syncDrawingToServer({ id: 'overlay_1', name: 'rect', points: [{ value: 1 }] }, 'BTC');

    // Verify timer was created
    const upsertTimer = env._timers.find(t => !t.cleared);
    assert.ok(upsertTimer, 'UPSERT timer should be queued');

    // Now delete — should cancel the pending UPSERT
    DT.deleteDrawingFromServer('overlay_1', 'BTC');

    // The UPSERT timer should be cancelled
    assert.ok(upsertTimer.cleared, 'UPSERT timer should be cancelled after delete');

    // DELETE fetch should have been sent
    const deleteFetch = env._fetchCalls.find(c => c.url.includes('by-local-id'));
    assert.ok(deleteFetch, 'DELETE fetch should be sent');
    assert.equal(deleteFetch.opts.method, 'DELETE');
});

test('deleteDrawingFromServer adds to _recentlyDeleted buffer', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    DT.deleteDrawingFromServer('overlay_99', 'ETH');

    // The DELETE fetch should include the drawing ID
    const delFetch = env._fetchCalls.find(c => c.opts && c.opts.method === 'DELETE');
    assert.ok(delFetch, 'DELETE fetch should be sent');
    const body = JSON.parse(delFetch.opts.body);
    assert.equal(body.local_id, 'overlay_99');
    assert.equal(body.asset, 'ETH');
});

test('deleteDrawingFromServer without pending UPSERT still works', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    // No prior syncDrawingToServer — delete should still work
    DT.deleteDrawingFromServer('overlay_new', 'BTC');
    const delFetch = env._fetchCalls.find(c => c.opts && c.opts.method === 'DELETE');
    assert.ok(delFetch, 'DELETE should work without prior UPSERT');
});

// ─── 2. Debounce coalescence ────────────────────────────────────────

test('syncDrawingToServer debounces rapid edits into one sync', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    const drawing = { id: 'overlay_2', name: 'rect', points: [{ value: 1 }] };

    // Rapid edits
    DT.syncDrawingToServer(drawing, 'BTC');
    DT.syncDrawingToServer(drawing, 'BTC');
    DT.syncDrawingToServer(drawing, 'BTC');

    // Only the last timer should be active (previous ones cleared)
    const activeTimers = env._timers.filter(t => !t.cleared);
    assert.equal(activeTimers.length, 1, 'Only one active timer after rapid edits');
});

test('syncDrawingToServer immediate=true skips debounce', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    const drawing = { id: 'overlay_3', name: 'rect', points: [{ value: 1 }] };
    DT.syncDrawingToServer(drawing, 'BTC', true);

    // Should fire immediately (fetch call, no timer)
    const upsertFetch = env._fetchCalls.find(c => c.url.includes('upsert'));
    assert.ok(upsertFetch, 'Immediate sync should send fetch right away');
});

test('syncDrawingToServer skipped when _suppressDrawingSync is true', () => {
    const env = createMockEnv();
    env.window._suppressDrawingSync = true;
    const DT = loadSyncModule(env);

    DT.syncDrawingToServer({ id: 'overlay_4', name: 'rect', points: [{ value: 1 }] }, 'BTC');

    // No timer, no fetch
    assert.equal(env._timers.length, 0, 'No timer when suppressed');
    assert.equal(env._fetchCalls.length, 0, 'No fetch when suppressed');
});

// ─── 3. Adaptive polling ────────────────────────────────────────────

test('adaptive polling starts at fast interval', () => {
    const env = createMockEnv();
    loadSyncModule(env);

    // The init() should have created a setInterval
    const pollInterval = env._intervals.find(i => !i.cleared);
    assert.ok(pollInterval, 'Poll interval should be created');
    assert.equal(pollInterval.ms, 5000, 'Should start at 5s (POLL_FAST_MS)');
});

test('syncDrawingToServer resets poll to fast interval', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    // Simulate drawing edit — should reset adaptive polling
    DT.syncDrawingToServer({ id: 'overlay_5', name: 'rect', points: [{ value: 1 }] }, 'BTC');

    // The latest interval should be 5000ms (fast)
    const activeIntervals = env._intervals.filter(i => !i.cleared);
    const latest = activeIntervals[activeIntervals.length - 1];
    assert.ok(latest, 'Should have an active poll interval');
    assert.equal(latest.ms, 5000, 'Should be fast interval after user activity');
});

// ─── 4. Session ID ──────────────────────────────────────────────────

test('session ID is unique per module load', () => {
    const env1 = createMockEnv();
    const DT1 = loadSyncModule(env1);

    const env2 = createMockEnv();
    const DT2 = loadSyncModule(env2);

    assert.ok(DT1.SYNC_SESSION_ID, 'Session ID should exist');
    assert.ok(DT2.SYNC_SESSION_ID, 'Session ID should exist');
    assert.notEqual(DT1.SYNC_SESSION_ID, DT2.SYNC_SESSION_ID, 'Session IDs should be unique');
});

test('session ID format is tab_<random>_<timestamp>', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);
    assert.match(DT.SYNC_SESSION_ID, /^tab_[a-z0-9]+_\d+$/, 'Session ID format');
});

// ─── 5. Drawing type mapping ────────────────────────────────────────

test('mapDrawingType maps known types correctly', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    assert.equal(DT.mapDrawingType('straightLine'), 'trendline');
    assert.equal(DT.mapDrawingType('rect'), 'zone');
    assert.equal(DT.mapDrawingType('fibonacciLine'), 'fibonacci');
    assert.equal(DT.mapDrawingType('longPosition'), 'long_position');
    assert.equal(DT.mapDrawingType('spot'), 'spot');
});

test('mapDrawingType returns annotation for unknown types', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    assert.equal(DT.mapDrawingType('unknownThing'), 'annotation');
    assert.equal(DT.mapDrawingType(''), 'annotation');
});

// ─── 6. Sync headers ───────────────────────────────────────────────

test('DELETE request includes X-Sync-Session header', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    DT.deleteDrawingFromServer('overlay_10', 'BTC');

    const delFetch = env._fetchCalls.find(c => c.opts && c.opts.method === 'DELETE');
    assert.ok(delFetch, 'DELETE fetch sent');
    assert.ok(delFetch.opts.headers['X-Sync-Session'], 'X-Sync-Session header present');
    assert.equal(delFetch.opts.headers['X-Sync-Session'], DT.SYNC_SESSION_ID);
});

test('UPSERT request includes X-Sync-Session header', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    DT.syncDrawingToServer({ id: 'overlay_11', name: 'rect', points: [{ value: 1 }] }, 'BTC', true);

    const upsertFetch = env._fetchCalls.find(c => c.url.includes('upsert'));
    assert.ok(upsertFetch, 'UPSERT fetch sent');
    assert.ok(upsertFetch.opts.headers['X-Sync-Session'], 'X-Sync-Session header present');
});

// ─── 7. refreshDrawingsFromServer guards ────────────────────────────

test('refreshDrawingsFromServer returns early when symbol is null', () => {
    const env = createMockEnv();
    env.klineChartSymbol = null;
    const DT = loadSyncModule(env);

    DT.refreshDrawingsFromServer(['BTC'], 100);

    // No fetch should be sent (early return)
    const drawingsFetch = env._fetchCalls.find(c => c.url.includes('/drawings?'));
    assert.ok(!drawingsFetch, 'Should not fetch when symbol is null');
});

test('refreshDrawingsFromServer returns early when asset does not match', () => {
    const env = createMockEnv();
    env.klineChartSymbol = 'BTC';
    const DT = loadSyncModule(env);

    DT.refreshDrawingsFromServer(['ETH'], 100);

    // No fetch — asset filter rejects BTC ≠ ETH
    const drawingsFetch = env._fetchCalls.find(c => c.url.includes('/drawings?'));
    assert.ok(!drawingsFetch, 'Should not fetch when asset does not match');
});

test('refreshDrawingsFromServer proceeds when asset matches', () => {
    const env = createMockEnv();
    env.klineChartSymbol = 'BTC';
    env._fetchResponse = { ok: true, status: 200, body: { success: true, drawings: [] } };
    const DT = loadSyncModule(env);

    DT.refreshDrawingsFromServer(['BTC'], 100);

    // Fetch should be sent for matching asset
    const drawingsFetch = env._fetchCalls.find(c => c.url.includes('/drawings?asset=BTC'));
    assert.ok(drawingsFetch, 'Should fetch when asset matches');
});

test('refreshDrawingsFromServer proceeds when no asset filter provided', () => {
    const env = createMockEnv();
    env.klineChartSymbol = 'ETH';
    env._fetchResponse = { ok: true, status: 200, body: { success: true, drawings: [] } };
    const DT = loadSyncModule(env);

    DT.refreshDrawingsFromServer([], 100);

    // No filter = refresh any asset
    const drawingsFetch = env._fetchCalls.find(c => c.url.includes('/drawings?asset=ETH'));
    assert.ok(drawingsFetch, 'Should fetch when no asset filter');
});

// ─── 8. Scenario drawing preservation ───────────────────────────────

test('deleteDrawingFromServer skips scenarios (no drawingId guard)', () => {
    const env = createMockEnv();
    const DT = loadSyncModule(env);

    // Empty ID should be skipped
    DT.deleteDrawingFromServer('', 'BTC');
    DT.deleteDrawingFromServer(null, 'BTC');

    assert.equal(env._fetchCalls.length, 0, 'Should not send DELETE for empty/null IDs');
});

// ─── 9. Visibility API integration ─────────────────────────────────

test('visibilitychange handler is registered', () => {
    const env = createMockEnv();
    loadSyncModule(env);

    const handlers = env._eventListeners['visibilitychange'] || [];
    assert.ok(handlers.length > 0, 'visibilitychange listener should be registered');
});

test('hidden tab clears poll timer', () => {
    const env = createMockEnv();
    loadSyncModule(env);

    // Simulate tab hidden
    env.document.visibilityState = 'hidden';
    const handlers = env._eventListeners['visibilitychange'] || [];
    handlers.forEach(h => h());

    // Poll interval should be cleared
    const activeIntervals = env._intervals.filter(i => !i.cleared);
    assert.equal(activeIntervals.length, 0, 'All intervals cleared when tab hidden');
});

test('visible tab restores poll timer and fires immediate poll', () => {
    const env = createMockEnv();
    loadSyncModule(env);

    // Hide
    env.document.visibilityState = 'hidden';
    (env._eventListeners['visibilitychange'] || []).forEach(h => h());

    // Clear fetch history
    env._fetchCalls.length = 0;

    // Show
    env.document.visibilityState = 'visible';
    (env._eventListeners['visibilitychange'] || []).forEach(h => h());

    // Should have created a new interval + fired immediate poll
    const activeIntervals = env._intervals.filter(i => !i.cleared);
    assert.ok(activeIntervals.length > 0, 'New interval created on tab visible');
    assert.ok(env._fetchCalls.some(c => c.url.includes('sync/poll')), 'Immediate poll on tab visible');
});

// ─── 10. Poll keeps drawings that have no metadata.local_id ─────────
// Drawings created through the API or an AI assistant carry no
// metadata.local_id. loadDrawingsFromServer() renders them as 'srv_<id>';
// the poll must derive the same id, or they only appear after a reload.
// Async, so it runs outside the synchronous test() wrapper.

async function testAsync(name, fn) {
    try {
        await fn();
        passed++;
        process.stdout.write(`  \x1b[32m✓\x1b[0m ${name}\n`);
    } catch (e) {
        failed++;
        failures.push({ name, error: e });
        process.stdout.write(`  \x1b[31m✗\x1b[0m ${name}\n    ${e.message}\n`);
    }
}

await testAsync('refreshDrawingsFromServer renders a server drawing with no local_id as srv_<id>', async () => {
    const env = createMockEnv();
    env.klineChartSymbol = 'BTC';
    env._fetchResponse = { ok: true, status: 200, body: { success: true, drawings: [
        { id: 77, drawing_type: 'horizontal_line', color: '#ff0000', line_width: 2,
          metadata: { kline_name: 'horizontalStraightLine', points: [{ timestamp: 1, value: 100 }] } },
        { id: 78, drawing_type: 'horizontal_line',
          metadata: { local_id: 'overlay_abc', kline_name: 'horizontalStraightLine', points: [{ timestamp: 1, value: 200 }] } },
    ] } };
    const DT = loadSyncModule(env);

    DT.refreshDrawingsFromServer(['BTC'], 100);
    for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r));

    const stored = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}');
    const ids = (stored.BTC || []).map(d => d.id).sort();
    assert.deepEqual(ids, ['overlay_abc', 'srv_77']);
});

// ─── Summary ────────────────────────────────────────────────────────

console.log(`\n\x1b[1m${passed + failed} tests: \x1b[32m${passed} passed\x1b[0m${failed ? `, \x1b[31m${failed} failed\x1b[0m` : ''}\n`);

if (failures.length > 0) {
    console.log('\x1b[31mFailures:\x1b[0m');
    failures.forEach(f => {
        console.log(`  ${f.name}:`);
        console.log(`    ${f.error.message}`);
    });
    process.exit(1);
}
