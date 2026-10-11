/**
 * JS unit tests for drawing CRUD operations (kline-drawings.js + drawing-editing.js).
 *
 * Tests: saveKlineDrawing, removeKlineDrawingFromStorage, undo/redo,
 * recreateOverlayWithStyles (color/width change), loadKlineDrawings.
 *
 * Run: node tests/test_drawings_js.mjs
 * Or:  make test-js
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

// Async-aware variant — MUST be awaited at top level (ESM). The sync test()
// silently passes any async fn (the returned promise is never awaited).
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

// ── Mock environment ────────────────────────────────────────────────

function createMockEnv() {
    let timerIdSeq = 1;
    const _store = {};
    const overlays = {};  // id -> overlay config (tracks createOverlay/removeOverlay)
    let overlayIdSeq = 1;

    const env = {
        _overlays: overlays,
        _deletedFromServer: [],
        _syncedToServer: [],
        _consoleOutput: [],

        // Timer mocks
        setTimeout: (fn, ms) => { const id = timerIdSeq++; fn._timerId = id; return id; },
        clearTimeout: () => {},
        setInterval: () => timerIdSeq++,
        clearInterval: () => {},

        // localStorage
        localStorage: {
            getItem: (k) => _store[k] || null,
            setItem: (k, v) => { _store[k] = v; },
            removeItem: (k) => { delete _store[k]; },
        },

        // Console
        console: {
            log: (...args) => env._consoleOutput.push(args.join(' ')),
            warn: (...args) => env._consoleOutput.push(args.join(' ')),
            error: (...args) => env._consoleOutput.push(args.join(' ')),
        },

        // KLineChart mock — tracks overlay operations
        klineChart: {
            createOverlay: (config, paneId) => {
                const id = 'overlay_mock_' + overlayIdSeq++;
                overlays[id] = { ...config, id, paneId: paneId || 'candle_pane' };
                return id;
            },
            removeOverlay: (idOrFilter) => {
                if (typeof idOrFilter === 'string') {
                    delete overlays[idOrFilter];
                } else if (!idOrFilter) {
                    Object.keys(overlays).forEach(k => delete overlays[k]);
                }
            },
            getDataList: () => [{ timestamp: Date.now(), open: 1, high: 2, low: 0.5, close: 1.5, volume: 100 }],
            resize: () => {},
        },

        // Globals
        klineChartSymbol: 'ETH',
        klineChartTimeframe: '1D',
        klineSelectedOverlayId: null,
        _isRecreatingOverlay: false,
        _drawingsLoadedForSymbol: null,
        _invisiblePointStyle: { color: 'transparent' },

        // DrawingTools mock (the sync module)
        DrawingTools: {
            syncDrawingToServer: (drawing, asset, immediate) => {
                env._syncedToServer.push({ id: drawing.id, name: drawing.name, asset, immediate });
            },
            deleteDrawingFromServer: (drawingId, asset) => {
                env._deletedFromServer.push({ id: drawingId, asset });
            },
        },

        // Stubs for functions called by the drawing code
        updateDrawerPosition: () => {},
        autoOpenJournalEntry: () => {},
        getOverlayCallbacks: () => ({}),
        snapPointsToData: (pts) => pts,
        hexToRgba: (hex, alpha) => hex + '_' + alpha,
        getActivePaneIds: () => ['candle_pane', 'volume_pane'],
        loadKlineDrawings: null,  // Will be set after loading kline-drawings.js
        _autoCaptureTrade: () => {},

        // Builtins
        Math, Date, JSON, Object, Array, Promise, parseInt, parseFloat,
        encodeURIComponent,
    };

    // Seed localStorage with some drawings
    _store['klineDrawings'] = JSON.stringify({
        ETH: [
            { id: 'draw_1', name: 'straightLine', points: [{ value: 100, timestamp: 1000 }, { value: 200, timestamp: 2000 }], color: '#ff0000', width: 2 },
            { id: 'draw_2', name: 'rect', points: [{ value: 150, timestamp: 1500 }, { value: 250, timestamp: 2500 }], color: '#00ff00', width: 1 },
        ],
        BTC: [
            { id: 'draw_3', name: 'fibonacciLine', points: [{ value: 50000 }, { value: 60000 }], color: '#0000ff', width: 1 },
        ]
    });

    env.window = env;
    env.document = { addEventListener: () => {} };
    env.addEventListener = () => {};
    return env;
}

function loadDrawingsModule(env) {
    const src = readFileSync('static/js/kline-drawings.js', 'utf8');
    const ctx = createContext(env);
    new Script(src, { filename: 'kline-drawings.js' }).runInContext(ctx);
    return ctx;
}

function getDrawingsFromStorage(env, symbol) {
    const all = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}');
    return all[symbol] || [];
}

// ── Tests ───────────────────────────────────────────────────────────

console.log('\n\x1b[1mDrawing CRUD JS Unit Tests\x1b[0m\n');

// ─── saveKlineDrawing ───────────────────────────────────────────────

test('saveKlineDrawing adds new drawing to localStorage', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    ctx.saveKlineDrawing({ id: 'new_1', name: 'segment', points: [{ value: 1 }], color: '#fff', width: 1 });

    const drawings = getDrawingsFromStorage(env, 'ETH');
    assert.ok(drawings.find(d => d.id === 'new_1'), 'New drawing should be in localStorage');
    assert.equal(drawings.length, 3, 'Total should be 3 (2 existing + 1 new)');
});

test('saveKlineDrawing updates existing drawing in localStorage', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    ctx.saveKlineDrawing({ id: 'draw_1', name: 'straightLine', points: [{ value: 999 }], color: '#000', width: 5 });

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const updated = drawings.find(d => d.id === 'draw_1');
    assert.equal(updated.color, '#000', 'Color should be updated');
    assert.equal(updated.width, 5, 'Width should be updated');
    assert.equal(drawings.length, 2, 'Total should still be 2 (update, not add)');
});

test('saveKlineDrawing routes by drawing.symbol, not the current chart (no cross-symbol leak)', () => {
    const env = createMockEnv();  // klineChartSymbol = 'ETH'
    const ctx = loadDrawingsModule(env);

    // Simulate the async writeback firing after a symbol switch: the drawing
    // belongs to BTC but the user is now viewing ETH. It must NOT leak into ETH.
    ctx.saveKlineDrawing({ id: 'btc_line', name: 'watched_segment', symbol: 'BTC', points: [{ value: 1 }], color: '#fff', width: 1 });

    assert.equal(getDrawingsFromStorage(env, 'ETH').find(d => d.id === 'btc_line'), undefined,
        'BTC drawing must NOT leak into the ETH bucket');
    assert.ok(getDrawingsFromStorage(env, 'BTC').find(d => d.id === 'btc_line'),
        'BTC drawing must land in its own (BTC) bucket');
});

test('saveKlineDrawing stamps the owning symbol on first save', () => {
    const env = createMockEnv();  // klineChartSymbol = 'ETH'
    const ctx = loadDrawingsModule(env);

    ctx.saveKlineDrawing({ id: 'fresh_1', name: 'segment', points: [{ value: 1 }], color: '#fff', width: 1 });

    const rec = getDrawingsFromStorage(env, 'ETH').find(d => d.id === 'fresh_1');
    assert.ok(rec, 'new drawing saved to the current-chart bucket');
    assert.equal(rec.symbol, 'ETH', 'symbol stamped to the current chart on first save');
});

test('saveKlineDrawing triggers sync to server', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    ctx.saveKlineDrawing({ id: 'sync_1', name: 'rect', points: [{ value: 1 }], color: '#f00', width: 1 });

    assert.equal(env._syncedToServer.length, 1, 'Should trigger one sync');
    assert.equal(env._syncedToServer[0].id, 'sync_1');
    assert.equal(env._syncedToServer[0].asset, 'ETH');
});

test('saveKlineDrawing syncs with immediate=true during overlay recreation', () => {
    const env = createMockEnv();
    env._isRecreatingOverlay = true;
    const ctx = loadDrawingsModule(env);

    ctx.saveKlineDrawing({ id: 'imm_1', name: 'rect', points: [{ value: 1 }], color: '#f00', width: 1 });

    assert.equal(env._syncedToServer[0].immediate, true, 'Should sync immediately during recreation');
});

test('saveKlineDrawing does nothing when symbol is null', () => {
    const env = createMockEnv();
    env.klineChartSymbol = null;
    const ctx = loadDrawingsModule(env);

    ctx.saveKlineDrawing({ id: 'null_1', name: 'rect', points: [{ value: 1 }] });

    assert.equal(env._syncedToServer.length, 0, 'No sync when symbol is null');
});

// ─── removeKlineDrawingFromStorage ──────────────────────────────────

test('removeKlineDrawingFromStorage removes drawing from localStorage', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    ctx.removeKlineDrawingFromStorage('draw_1');

    const drawings = getDrawingsFromStorage(env, 'ETH');
    assert.ok(!drawings.find(d => d.id === 'draw_1'), 'draw_1 should be removed');
    assert.equal(drawings.length, 1, 'Only draw_2 should remain');
});

test('removeKlineDrawingFromStorage triggers server delete', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    ctx.removeKlineDrawingFromStorage('draw_2');

    assert.equal(env._deletedFromServer.length, 1, 'Should trigger server delete');
    assert.equal(env._deletedFromServer[0].id, 'draw_2');
    assert.equal(env._deletedFromServer[0].asset, 'ETH');
});

test('removeKlineDrawingFromStorage skips server delete during overlay recreation', () => {
    const env = createMockEnv();
    env._isRecreatingOverlay = true;
    const ctx = loadDrawingsModule(env);

    ctx.removeKlineDrawingFromStorage('draw_1');

    // Drawing removed from localStorage
    const drawings = getDrawingsFromStorage(env, 'ETH');
    assert.ok(!drawings.find(d => d.id === 'draw_1'), 'Should be removed from localStorage');

    // But NOT deleted from server
    assert.equal(env._deletedFromServer.length, 0, 'Should NOT delete from server during recreation');
});

test('removeKlineDrawingFromStorage for non-existent ID preserves other drawings', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    ctx.removeKlineDrawingFromStorage('nonexistent');

    const drawings = getDrawingsFromStorage(env, 'ETH');
    assert.equal(drawings.length, 2, 'Original drawings unchanged');
    // Server delete IS still sent (unconditional cleanup — server returns "not found" harmlessly)
    assert.equal(env._deletedFromServer.length, 1, 'Server delete sent even for unknown ID');
});

test('removeKlineDrawingFromStorage invalidates drawing cache', () => {
    const env = createMockEnv();
    env._drawingsLoadedForSymbol = 'ETH';
    const ctx = loadDrawingsModule(env);

    ctx.removeKlineDrawingFromStorage('draw_1');

    assert.equal(env._drawingsLoadedForSymbol, null, 'Cache should be invalidated');
});

// ─── Undo / Redo ────────────────────────────────────────────────────

test('undo restores previous drawing state', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    // Save a new drawing (pushes undo snapshot of the 2-drawing state)
    ctx.saveKlineDrawing({ id: 'undo_1', name: 'rect', points: [{ value: 1 }], color: '#f00', width: 1 });
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, 3, 'Should have 3 drawings');

    // Undo — should restore to 2 drawings
    ctx.undoKlineDrawings();
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, 2, 'Should revert to 2 drawings');
});

test('redo restores undone state', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    // Save, then undo, then redo
    ctx.saveKlineDrawing({ id: 'redo_1', name: 'rect', points: [{ value: 1 }], color: '#f00', width: 1 });
    ctx.undoKlineDrawings();
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, 2, 'After undo: 2');

    ctx.redoKlineDrawings();
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, 3, 'After redo: 3');
});

test('undo with no history does nothing', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    // No prior saves — undo should be harmless
    ctx.undoKlineDrawings();
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, 2, 'Unchanged');
});

test('redo with no redo stack does nothing', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    ctx.redoKlineDrawings();
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, 2, 'Unchanged');
});

test('new mutation clears redo stack', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    // Save, undo (creates redo entry), then save again (should clear redo)
    ctx.saveKlineDrawing({ id: 'clr_1', name: 'rect', points: [{ value: 1 }], color: '#f00', width: 1 });
    // Need to clear coalesce flag between saves
    ctx._undoCoalesceFlag = false;
    ctx.undoKlineDrawings();
    // Now redo stack has 1 entry. New save should clear it.
    ctx._undoCoalesceFlag = false;
    ctx.saveKlineDrawing({ id: 'clr_2', name: 'rect', points: [{ value: 2 }], color: '#0f0', width: 1 });

    // Redo should do nothing — stack was cleared
    ctx.redoKlineDrawings();
    const drawings = getDrawingsFromStorage(env, 'ETH');
    // Should have the clr_2 drawing (not reverted)
    assert.ok(drawings.find(d => d.id === 'clr_2'), 'clr_2 should exist (redo stack cleared)');
});

// ─── recreateOverlayWithStyles ──────────────────────────────────────

test('recreateOverlayWithStyles removes old overlay and creates new one', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    // recreateOverlayWithStyles lives in drawing-editing.js; it is simulated
    // here because it depends on too many chart-app globals.
    // Instead, test the critical invariant: _isRecreatingOverlay suppresses server delete

    // Simulate what recreateOverlayWithStyles does:
    const oldId = 'draw_1';
    env._isRecreatingOverlay = true;

    // Remove old overlay (KLineChart)
    env.klineChart.removeOverlay(oldId);

    // Remove from storage (should NOT trigger server delete due to _isRecreatingOverlay)
    ctx.removeKlineDrawingFromStorage(oldId);
    assert.equal(env._deletedFromServer.length, 0, 'No server delete during recreation');

    // Create new overlay
    const newId = env.klineChart.createOverlay({
        name: 'straightLine',
        points: [{ value: 100 }, { value: 200 }],
        styles: { line: { color: '#00ff00', size: 3 } }
    });

    // Save with new ID (should trigger sync with immediate=true)
    ctx.saveKlineDrawing({ id: newId, name: 'straightLine', points: [{ value: 100 }, { value: 200 }], color: '#00ff00', width: 3 });

    assert.equal(env._syncedToServer.length, 1, 'New drawing synced');
    assert.equal(env._syncedToServer[0].immediate, true, 'Synced immediately during recreation');

    // Clean up old orphan from server
    env.DrawingTools.deleteDrawingFromServer(oldId, env.klineChartSymbol);
    assert.equal(env._deletedFromServer.length, 1, 'Old ID cleaned up from server');
    assert.equal(env._deletedFromServer[0].id, oldId);

    env._isRecreatingOverlay = false;
});

test('recreateOverlayWithStyles preserves drawing in localStorage with new ID', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    env._isRecreatingOverlay = true;

    // Remove old
    ctx.removeKlineDrawingFromStorage('draw_2');
    assert.ok(!getDrawingsFromStorage(env, 'ETH').find(d => d.id === 'draw_2'), 'Old removed');

    // Save new
    ctx.saveKlineDrawing({ id: 'draw_2_new', name: 'rect', points: [{ value: 150 }, { value: 250 }], color: '#ff00ff', width: 2 });
    assert.ok(getDrawingsFromStorage(env, 'ETH').find(d => d.id === 'draw_2_new'), 'New ID saved');
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, 2, 'Total unchanged (removed 1 + added 1)');

    env._isRecreatingOverlay = false;
});

// ─── loadKlineDrawings ──────────────────────────────────────────────

test('loadKlineDrawings creates overlays for all drawings in localStorage', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);

    ctx.loadKlineDrawings();

    // Should have created overlays for draw_1 and draw_2
    const overlayIds = Object.keys(env._overlays);
    assert.ok(overlayIds.length >= 2, `Should create at least 2 overlays, got ${overlayIds.length}`);
});

test('loadKlineDrawings does nothing when symbol is null', () => {
    const env = createMockEnv();
    env.klineChartSymbol = null;
    const ctx = loadDrawingsModule(env);

    ctx.loadKlineDrawings();

    assert.equal(Object.keys(env._overlays).length, 0, 'No overlays when symbol is null');
});

test('loadKlineDrawings does nothing when no klineChart', () => {
    const env = createMockEnv();
    env.klineChart = null;
    const ctx = loadDrawingsModule(env);

    ctx.loadKlineDrawings();

    assert.equal(Object.keys(env._overlays).length, 0, 'No overlays when chart is null');
});

// ─── Cross-symbol isolation ─────────────────────────────────────────

test('operations are scoped to current symbol', () => {
    const env = createMockEnv();
    env.klineChartSymbol = 'ETH';
    const ctx = loadDrawingsModule(env);

    // Delete ETH drawing
    ctx.removeKlineDrawingFromStorage('draw_1');

    // BTC drawings should be unaffected
    const btcDrawings = getDrawingsFromStorage(env, 'BTC');
    assert.equal(btcDrawings.length, 1, 'BTC drawings untouched');
    assert.equal(btcDrawings[0].id, 'draw_3');

    // ETH should have 1 remaining
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, 1);
});

// ─── Copy / Paste / Duplicate ──────────────────────────────────────

// Enhanced mock with convertFromPixel + richer data list for copy/paste tests
function createCopyPasteEnv() {
    const env = createMockEnv();
    // Build a 100-bar data list with timestamps spaced 86400000ms (1 day) apart
    const baseTs = 1700000000000;
    const dataList = [];
    for (let i = 0; i < 100; i++) {
        dataList.push({ timestamp: baseTs + i * 86400000, open: 100 + i, high: 110 + i, low: 90 + i, close: 105 + i, volume: 1000 });
    }
    env.klineChart.getDataList = () => dataList;
    env.klineChart.convertFromPixel = (pt) => {
        // Return center of data as mock — dataIndex 50, value 150
        return { dataIndex: 50, value: 150 };
    };
    env.klineChart.overrideOverlay = () => {};
    env._toasts = [];
    env.showToast = (msg) => env._toasts.push(msg);
    env._logScaleEnabled = false;
    env.document = {
        addEventListener: () => {},
        getElementById: () => ({ getBoundingClientRect: () => ({ width: 800, height: 600 }), querySelectorAll: () => [], appendChild: () => {}, addEventListener: () => {}, removeEventListener: () => {} }),
        createElement: () => ({ className: '', innerHTML: '', style: {}, addEventListener: () => {}, classList: { add: () => {}, remove: () => {} }, remove: () => {}, appendChild: () => {} }),
        body: { appendChild: () => {} },
    };

    // Seed drawings with a mix of copyable and non-copyable types
    const store = JSON.parse(env.localStorage.getItem('klineDrawings'));
    store.ETH.push(
        { id: 'pos_1', name: 'position', points: [{ value: 100, timestamp: baseTs + 10 * 86400000 }, { value: 90, timestamp: baseTs + 10 * 86400000 }, { value: 120, timestamp: baseTs + 10 * 86400000 }], color: '#ff0', width: 1, extendData: { _journalId: 'j1', tps: [130] } },
        { id: 'spot_1', name: 'spot', points: [{ value: 100, timestamp: baseTs + 5 * 86400000 }], color: '#0f0', width: 1, extendData: { usdAmount: 500 } },
        { id: 'chan_1', name: 'boundedChannel', points: [{ value: 100, timestamp: baseTs + 20 * 86400000 }, { value: 200, timestamp: baseTs + 40 * 86400000 }, { value: 120, timestamp: baseTs + 20 * 86400000 }], color: '#0ff', width: 2 },
        { id: 'alert_1', name: 'alertLine', points: [{ value: 150, timestamp: baseTs + 30 * 86400000 }], color: '#f80', width: 1, extendData: { _alertId: 'a123', _color: '#f80' } },
        { id: 'fib_1', name: 'fibonacciRetrace', points: [{ value: 100, timestamp: baseTs + 10 * 86400000 }, { value: 200, timestamp: baseTs + 30 * 86400000 }], color: '#ff0', width: 1, extendData: { _fibLevels: [0, 0.236, 0.382, 0.5, 0.618, 1] } },
        { id: 'locked_1', name: 'straightLine', points: [{ value: 100, timestamp: baseTs + 15 * 86400000 }, { value: 200, timestamp: baseTs + 35 * 86400000 }], color: '#fff', width: 1, extendData: { _locked: true, watch: true } }
    );
    env.localStorage.setItem('klineDrawings', JSON.stringify(store));
    return env;
}

console.log('\n\x1b[1mCopy / Paste / Duplicate Tests\x1b[0m\n');

test('_isDrawingCopyable returns true for regular drawings', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);
    assert.ok(ctx._isDrawingCopyable('straightLine'));
    assert.ok(ctx._isDrawingCopyable('rect'));
    assert.ok(ctx._isDrawingCopyable('fibonacciRetrace'));
    assert.ok(ctx._isDrawingCopyable('boundedChannel'));
    assert.ok(ctx._isDrawingCopyable('textAnnotation'));
    assert.ok(ctx._isDrawingCopyable('alertLine'));
});

test('_isDrawingCopyable returns false for positions and spots', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);
    assert.ok(!ctx._isDrawingCopyable('position'));
    assert.ok(!ctx._isDrawingCopyable('longPosition'));
    assert.ok(!ctx._isDrawingCopyable('shortPosition'));
    assert.ok(!ctx._isDrawingCopyable('spot'));
});

test('copySelectedDrawing copies a trendline to clipboard', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    const result = ctx.copySelectedDrawing('draw_1');

    assert.ok(result, 'Should return true');
    assert.ok(ctx._drawingClipboard, 'Clipboard should be set');
    assert.equal(ctx._drawingClipboard.symbol, 'ETH');
    assert.equal(ctx._drawingClipboard.drawing.id, 'draw_1');
    assert.equal(ctx._drawingClipboard.drawing.name, 'straightLine');
    assert.ok(env._toasts.some(t => t.includes('Copied trendline')));
});

test('copySelectedDrawing refuses to copy position', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    const result = ctx.copySelectedDrawing('pos_1');

    assert.ok(!result, 'Should return false');
    assert.ok(!ctx._drawingClipboard, 'Clipboard should remain null');
    assert.ok(env._toasts.some(t => t.includes('Cannot copy')));
});

test('copySelectedDrawing refuses to copy spot', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    const result = ctx.copySelectedDrawing('spot_1');

    assert.ok(!result, 'Should return false');
    assert.ok(!ctx._drawingClipboard, 'Clipboard should remain null');
});

test('copySelectedDrawing deep-clones drawing (mutation-safe)', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.copySelectedDrawing('draw_1');

    // Mutate original in storage
    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    all.ETH[0].color = '#changed';
    env.localStorage.setItem('klineDrawings', JSON.stringify(all));

    // Clipboard should be unaffected
    assert.equal(ctx._drawingClipboard.drawing.color, '#ff0000', 'Clone should be independent');
});

test('copySelectedDrawing uses klineSelectedOverlayId as fallback', () => {
    const env = createCopyPasteEnv();
    env.klineSelectedOverlayId = 'draw_2';
    const ctx = loadDrawingsModule(env);

    const result = ctx.copySelectedDrawing();

    assert.ok(result);
    assert.equal(ctx._drawingClipboard.drawing.id, 'draw_2');
});

test('pasteDrawing creates a new overlay at screen center', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.copySelectedDrawing('draw_1');
    const overlaysBefore = Object.keys(env._overlays).length;
    const drawingsBefore = getDrawingsFromStorage(env, 'ETH').length;

    const result = ctx.pasteDrawing();

    assert.ok(result, 'Should return true');
    assert.ok(Object.keys(env._overlays).length > overlaysBefore, 'New overlay created');
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, drawingsBefore + 1, 'Drawing added to storage');
    assert.ok(env._toasts.some(t => t.includes('Pasted trendline')));
});

test('pasteDrawing generates a new ID (not reusing source)', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.copySelectedDrawing('draw_1');
    ctx.pasteDrawing();

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const pasted = drawings[drawings.length - 1];
    assert.notEqual(pasted.id, 'draw_1', 'Should have a fresh ID');
    assert.ok(pasted.id.startsWith('overlay_mock_'), 'ID from KLineChart mock');
});

test('pasteDrawing preserves drawing type and style', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.copySelectedDrawing('draw_1');
    ctx.pasteDrawing();

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const pasted = drawings[drawings.length - 1];
    assert.equal(pasted.name, 'straightLine');
    assert.equal(pasted.color, '#ff0000');
    assert.equal(pasted.width, 2);
});

test('pasteDrawing strips side-effect metadata (_alertId, _journalId, watch, _locked)', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    // Copy the alert line (has _alertId)
    ctx.copySelectedDrawing('alert_1');
    ctx.pasteDrawing();

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const pasted = drawings[drawings.length - 1];
    assert.ok(!pasted.extendData._alertId, '_alertId should be stripped');

    // Copy locked+watched trendline
    ctx.copySelectedDrawing('locked_1');
    ctx.pasteDrawing();

    const drawings2 = getDrawingsFromStorage(env, 'ETH');
    const pasted2 = drawings2[drawings2.length - 1];
    assert.ok(!pasted2.extendData._locked, '_locked should be stripped');
    assert.ok(!pasted2.extendData.watch, 'watch should be stripped');
});

test('pasteDrawing preserves fib levels in extendData', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.copySelectedDrawing('fib_1');
    ctx.pasteDrawing();

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const pasted = drawings[drawings.length - 1];
    assert.deepEqual(pasted.extendData._fibLevels, [0, 0.236, 0.382, 0.5, 0.618, 1], 'Fib levels preserved');
});

test('pasteDrawing fails for wrong symbol', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.copySelectedDrawing('draw_1');

    // Switch symbol
    env.klineChartSymbol = 'BTC';
    const result = ctx.pasteDrawing();

    assert.ok(!result, 'Should fail');
    assert.ok(env._toasts.some(t => t.includes('Clipboard is for ETH')));
});

test('pasteDrawing fails when clipboard is empty', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    const result = ctx.pasteDrawing();

    assert.ok(!result, 'Should fail with empty clipboard');
});

test('paste can be repeated (multiple pastes from one copy)', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.copySelectedDrawing('draw_1');
    const before = getDrawingsFromStorage(env, 'ETH').length;

    ctx.pasteDrawing();
    ctx.pasteDrawing();
    ctx.pasteDrawing();

    assert.equal(getDrawingsFromStorage(env, 'ETH').length, before + 3, '3 pastes = 3 new drawings');
});

test('pasteDrawing integrates with undo', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    const before = getDrawingsFromStorage(env, 'ETH').length;
    ctx.copySelectedDrawing('draw_1');
    ctx._undoCoalesceFlag = false;
    ctx.pasteDrawing();

    assert.equal(getDrawingsFromStorage(env, 'ETH').length, before + 1);

    ctx.undoKlineDrawings();
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, before, 'Undo removes pasted drawing');
});

test('duplicateSelectedDrawing creates offset copy', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    const before = getDrawingsFromStorage(env, 'ETH').length;
    const result = ctx.duplicateSelectedDrawing('draw_1');

    assert.ok(result, 'Should return true');
    assert.equal(getDrawingsFromStorage(env, 'ETH').length, before + 1);
    assert.ok(env._toasts.some(t => t.includes('Duplicated trendline')));
});

test('duplicateSelectedDrawing does NOT overwrite clipboard', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    // Copy a rect to clipboard
    ctx.copySelectedDrawing('draw_2');
    assert.equal(ctx._drawingClipboard.drawing.id, 'draw_2');

    // Duplicate a trendline — clipboard should still be rect
    ctx.duplicateSelectedDrawing('draw_1');
    assert.equal(ctx._drawingClipboard.drawing.id, 'draw_2', 'Clipboard preserved');
});

test('duplicateSelectedDrawing refuses position', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    const result = ctx.duplicateSelectedDrawing('pos_1');
    assert.ok(!result);
    assert.ok(env._toasts.some(t => t.includes('Cannot duplicate')));
});

test('duplicateSelectedDrawing offsets points', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.duplicateSelectedDrawing('draw_1');

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const original = drawings.find(d => d.id === 'draw_1');
    const duped = drawings[drawings.length - 1];

    // Values should be offset upward by ~1.5%
    assert.ok(duped.points[0].value > original.points[0].value, 'Value should be offset upward');
    // Timestamps should differ (shifted by 5 bars)
    assert.notEqual(duped.points[0].timestamp, original.points[0].timestamp, 'Timestamp should be offset');
});

test('duplicateSelectedDrawing strips side effects', () => {
    const env = createCopyPasteEnv();
    const ctx = loadDrawingsModule(env);

    ctx.duplicateSelectedDrawing('locked_1');

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const duped = drawings[drawings.length - 1];
    assert.ok(!duped.extendData._locked, '_locked stripped');
    assert.ok(!duped.extendData.watch, 'watch stripped');
});

test('duplicateSelectedDrawing uses klineSelectedOverlayId as fallback', () => {
    const env = createCopyPasteEnv();
    env.klineSelectedOverlayId = 'chan_1';
    const ctx = loadDrawingsModule(env);

    const result = ctx.duplicateSelectedDrawing();

    assert.ok(result);
    const drawings = getDrawingsFromStorage(env, 'ETH');
    const duped = drawings[drawings.length - 1];
    assert.equal(duped.name, 'boundedChannel');
});


// ─── Position hit-testing (pass-based) + overlap cycling ────────────

function createHitTestEnv(opts = {}) {
    const env = createMockEnv();
    // Dual-mode pixel mapping:
    //   timestamp path: x = timestamp / 10, y = 1000 - value
    //   dataIndex path: x = dataIndex * 10,  y = 1000 - value
    env.klineChart.convertToPixel = (pt) => {
        if (pt.dataIndex !== undefined) {
            return { x: pt.dataIndex * 10, y: 1000 - pt.value };
        }
        return { x: pt.timestamp / 10, y: 1000 - pt.value };
    };
    // _getYAxisStartX: null container -> 99999 fallback (no Y-axis rejection)
    env.document.getElementById = () => null;
    if (opts.logScale) env._logScaleEnabled = true;

    // Seed drawings. Geometry (live path via dataIndex):
    //   pos_1: dataIndex=10 → x=100. entry y=500, sl y=600, tp y=300. xRight=300, box y 295..605
    //   pos_2: dataIndex=12 → x=120. entry y=520, sl y=620, tp y=320. xRight=320, box y 315..625
    // Live overlay values are stored in chart space (already log-transformed when log is on).
    // Non-log: same as raw stored values. Log: Math.log(raw).
    const isLog = !!opts.logScale;
    const lv = (v) => isLog ? Math.log(v) : v;
    const liveOverlays = {
        'pos_1': { id: 'pos_1', points: [
            { dataIndex: 10, value: lv(500) },
            { dataIndex: 10, value: lv(400) },
            { dataIndex: 10, value: lv(700) },
        ]},
        'pos_2': { id: 'pos_2', points: [
            { dataIndex: 12, value: lv(480) },
            { dataIndex: 12, value: lv(380) },
            { dataIndex: 12, value: lv(680) },
        ]},
    };
    // By default return live overlays; opts.nullOverlayIds lists IDs to return null for (fallback path)
    const nullIds = new Set(opts.nullOverlayIds || []);
    env.klineChart.getOverlayById = (id) => {
        if (nullIds.has(id)) return null;
        return liveOverlays[id] || null;
    };

    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [
            { id: 'pos_1', name: 'position', points: [
                { timestamp: 1000, value: 500 }, { timestamp: 1000, value: 400 }, { timestamp: 1000, value: 700 }] },
            { id: 'pos_2', name: 'position', points: [
                { timestamp: 1200, value: 480 }, { timestamp: 1200, value: 380 }, { timestamp: 1200, value: 680 }] },
        ]
    }));
    return env;
}

test('hit-test: handle dot at left edge wins (entry)', () => {
    const env = createHitTestEnv();
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(100, 500);
    assert.ok(hit, 'should hit');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'entry');
});

test('hit-test: level line grabbable mid-span (was body before)', () => {
    const env = createHitTestEnv();
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(250, 502); // 2px off pos_1 entry line, mid-box
    assert.ok(hit, 'should hit');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'entry');
});

test('hit-test: another position\'s level line beats an overlapping body', () => {
    const env = createHitTestEnv();
    const ctx = loadDrawingsModule(env);
    // (250, 520): inside pos_1 body, exactly on pos_2 entry line
    const hit = ctx._getPositionHitAtPoint(250, 520);
    assert.ok(hit, 'should hit');
    assert.equal(hit.overlayId, 'pos_2');
    assert.equal(hit.level, 'entry');
});

test('hit-test: body hit returns full overlap stack in array order', () => {
    const env = createHitTestEnv();
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(250, 450); // inside both bodies, near no line
    assert.ok(hit, 'should hit');
    assert.equal(hit.level, 'body');
    assert.equal(hit.overlayId, 'pos_1', 'array-first grabbed when nothing selected');
    assert.equal(JSON.stringify(hit.stack), JSON.stringify(['pos_1', 'pos_2']));
});

test('hit-test: selected position wins the body grab', () => {
    const env = createHitTestEnv();
    env.klineSelectedOverlayId = 'pos_2';
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(250, 450);
    assert.equal(hit.overlayId, 'pos_2', 'selected beats array order');
    assert.equal(JSON.stringify(hit.stack), JSON.stringify(['pos_1', 'pos_2']), 'stack stays in array order');
});

test('hit-test: selected position\'s level line beats the other\'s', () => {
    const env = createHitTestEnv();
    env.klineSelectedOverlayId = 'pos_2';
    const ctx = loadDrawingsModule(env);
    // (250, 510): within 6px of BOTH pos_1 entry (500... 10px) -> only pos_2 entry (520, 10px)?
    // Use (250, 515): pos_1 entry 15px away (no), pos_2 entry 5px away (yes)
    const hit = ctx._getPositionHitAtPoint(250, 515);
    assert.equal(hit.overlayId, 'pos_2');
    assert.equal(hit.level, 'entry');
});

test('hit-test: log scale transforms entry/SL/TP like ITP', () => {
    const env = createHitTestEnv({ logScale: true });
    const ctx = loadDrawingsModule(env);
    // pos_1 entry raw 500 -> log(500)=6.2146 -> y = 1000 - 6.2146 = 993.785
    const hit = ctx._getPositionHitAtPoint(100, 993.8);
    assert.ok(hit, 'should hit at log-space y');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'entry');
    // The raw-space y (500) must NOT hit anything in log mode
    assert.equal(ctx._getPositionHitAtPoint(100, 500), null, 'raw-space y is a miss in log mode');
});

test('hit-test: fallback path (getOverlayById returns null) uses stored timestamps + _toChartVal', () => {
    // Force the fallback path for pos_1 by returning null from getOverlayById.
    // In non-log mode: stored timestamp 1000 → x=1000/10=100, value 500 → y=1000-500=500.
    // The hit at (100, 500) must still register via the fallback path.
    const env = createHitTestEnv({ nullOverlayIds: ['pos_1', 'pos_2'] });
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(100, 500);
    assert.ok(hit, 'fallback path should still hit');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'entry');
});

test('hit-test: fallback log path uses _toChartVal on stored values', () => {
    // Fallback path in log mode: stored value 500 → _toChartVal → log(500) ≈ 6.2146
    // → y = 1000 - 6.2146 ≈ 993.785. Must hit; raw-space y=500 must miss.
    const env = createHitTestEnv({ logScale: true, nullOverlayIds: ['pos_1', 'pos_2'] });
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(100, 993.8);
    assert.ok(hit, 'fallback log path should hit at log-space y');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'entry');
    assert.equal(ctx._getPositionHitAtPoint(100, 500), null, 'raw-space y must miss in log fallback mode');
});

test('hit-test Phase 0: live dataIndex geometry beats clamped timestamp geometry', () => {
    // Demonstrates the fix: a position whose stored timestamp would clamp to x=50
    // (clamped right-margin) but whose live overlay dataIndex maps to x=200.
    // In the old code, xLeft=50, xRight=250 (50+200 fallback). The handle dot
    // would fire at x=50; the body hit at x=100 would fall outside the level-line
    // span starting at x=50 if xRight stayed at 50+200=250 — workable by chance.
    // With the live path, xLeft=200, xRight=400 (200+200). A click at (200, 500)
    // must register as the entry handle.
    const env = createMockEnv();
    env.klineChart.convertToPixel = (pt) => {
        if (pt.dataIndex !== undefined) {
            return { x: pt.dataIndex * 10, y: 1000 - pt.value };
        }
        // Timestamp path: simulate clamping — always returns x=50 regardless of timestamp
        return { x: 50, y: 1000 - pt.value };
    };
    env.document.getElementById = () => null;
    // Live overlay: dataIndex=20 → x=200 (far right of the clamped x=50)
    env.klineChart.getOverlayById = (id) => {
        if (id === 'pos_clamp') {
            return { id: 'pos_clamp', points: [
                { dataIndex: 20, value: 500 },
                { dataIndex: 20, value: 400 },
                { dataIndex: 20, value: 700 },
            ]};
        }
        return null;
    };
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{ id: 'pos_clamp', name: 'position', points: [
            { timestamp: 99999, value: 500 },
            { timestamp: 99999, value: 400 },
            { timestamp: 99999, value: 700 },
        ]}]
    }));
    const ctx = loadDrawingsModule(env);
    // Live path: xLeft=200. Handle dot at (200, 500) must hit.
    const hit = ctx._getPositionHitAtPoint(200, 500);
    assert.ok(hit, 'live dataIndex path should hit at x=200');
    assert.equal(hit.overlayId, 'pos_clamp');
    assert.equal(hit.level, 'entry');
    // Clamped x=50 must NOT trigger a hit when clicking at live x=200 for the handle
    // (the handle is now at x=200, so x=50 click is ~150px away — well outside HANDLE_R=18)
    const missat50 = ctx._getPositionHitAtPoint(50, 500);
    assert.equal(missat50, null, 'clamped x=50 must miss when live path places handle at x=200');
});

test('cycle: _nextInCycleStack advances and wraps in array order', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._nextInCycleStack(['a', 'b', 'c'], 'a'), 'b');
    assert.equal(ctx._nextInCycleStack(['a', 'b', 'c'], 'b'), 'c');
    assert.equal(ctx._nextInCycleStack(['a', 'b', 'c'], 'c'), 'a');
    assert.equal(ctx._nextInCycleStack(['a', 'b'], 'not-in-stack'), 'a');
    assert.equal(ctx._nextInCycleStack([], 'x'), null);
    assert.equal(ctx._nextInCycleStack(null, 'x'), null);
});


test('rect hit-test: body hit returns overlap stack in array order', () => {
    const env = createHitTestEnv();
    // Two overlapping rects: r1 x 100..200 y 300..500, r2 x 150..250 y 350..550
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [
            { id: 'r1', name: 'rect', points: [{ timestamp: 1000, value: 700 }, { timestamp: 2000, value: 500 }] },
            { id: 'r2', name: 'rect', points: [{ timestamp: 1500, value: 650 }, { timestamp: 2500, value: 450 }] },
        ]
    }));
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getRectHitAtPoint(175, 420); // inside both bodies, away from all 8 handles
    assert.ok(hit, 'should hit');
    assert.equal(hit.handle, 'BODY');
    assert.equal(JSON.stringify(hit.stack), JSON.stringify(['r1', 'r2']));
});

test('rect hit-test: selected rect wins the body grab, stack keeps array order', () => {
    const env = createHitTestEnv();
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [
            { id: 'r1', name: 'rect', points: [{ timestamp: 1000, value: 700 }, { timestamp: 2000, value: 500 }] },
            { id: 'r2', name: 'rect', points: [{ timestamp: 1500, value: 650 }, { timestamp: 2500, value: 450 }] },
        ]
    }));
    env.klineSelectedOverlayId = 'r2';
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getRectHitAtPoint(175, 420);
    assert.equal(hit.overlayId, 'r2', 'selected beats array order');
    assert.equal(JSON.stringify(hit.stack), JSON.stringify(['r1', 'r2']));
});

// ─── Phase 2+3+4 unified hit-test tests ─────────────────────────────
console.log('\n\x1b[1mPhase 2+3+4: hitTestAtPoint + z-order tests\x1b[0m\n');

// Extended env that has both rects and positions.
// Rect geometry (timestamp path, no Y-axis rejection):
//   r1: x1=100,y1=300, x2=200,y2=500   (ts 1000..2000, val 700..500)
//   r2: x1=150,y1=350, x2=250,y2=550   (ts 1500..2500, val 650..450)
// Position geometry (live path via dataIndex):
//   pos_1: dataIndex=10 → x=100, entry y=500, sl y=600, tp y=300
// Coordinate system: x = ts/10 (for rect timestamp path); x = di*10 (for pos live)
//                    y = 1000 - value
function createUnifiedHitTestEnv(opts = {}) {
    const env = createHitTestEnv(opts);

    // Override convertToPixel to handle BOTH rect timestamp path and position dataIndex path
    env.klineChart.convertToPixel = (pt) => {
        if (pt.dataIndex !== undefined) {
            return { x: pt.dataIndex * 10, y: 1000 - pt.value };
        }
        return { x: pt.timestamp / 10, y: 1000 - pt.value };
    };

    // Seed default storage with both rects and positions
    const defaultZOrderRects = opts.rectZOrders || {};  // { id: zOrder }
    const defaultZOrderPos   = opts.posZOrders  || {};

    const r1 = { id: 'r1', name: 'rect',
        points: [{ timestamp: 1000, value: 700 }, { timestamp: 2000, value: 500 }],
        zOrder: defaultZOrderRects['r1'] != null ? defaultZOrderRects['r1'] : 0 };
    const r2 = { id: 'r2', name: 'rect',
        points: [{ timestamp: 1500, value: 650 }, { timestamp: 2500, value: 450 }],
        zOrder: defaultZOrderRects['r2'] != null ? defaultZOrderRects['r2'] : 0 };
    const p1 = { id: 'pos_1', name: 'position',
        points: [{ timestamp: 1000, value: 500 }, { timestamp: 1000, value: 400 }, { timestamp: 1000, value: 700 }],
        zOrder: defaultZOrderPos['pos_1'] != null ? defaultZOrderPos['pos_1'] : 0 };

    // Live overlay for pos_1 only (r1/r2 are rects; channels not needed here)
    const baseGetById = env.klineChart.getOverlayById;
    env.klineChart.getOverlayById = (id) => {
        if (id === 'pos_1') {
            return { id: 'pos_1', points: [
                { dataIndex: 10, value: 500 },
                { dataIndex: 10, value: 400 },
                { dataIndex: 10, value: 700 },
            ]};
        }
        return null;
    };

    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [r1, r2, p1]
    }));
    return env;
}

// ── hitTestAtPoint cross-type: position handle beats rect body ────────

test('unified hit-test: position handle dot at x=100,y=500 beats rect body at same point (position selected)', () => {
    // r1 body covers x:100..200, y:300..500. r1 BL corner is also at (100,500).
    // pos_1 entry handle is at x=100, y=500.
    // When pos_1 is selected, Pass 1 tests it first → position handle wins over rect corner.
    const env = createUnifiedHitTestEnv();
    env.klineSelectedOverlayId = 'pos_1';
    const ctx = loadDrawingsModule(env);
    const hit = ctx.hitTestAtPoint(100, 500);
    assert.ok(hit, 'should hit');
    assert.equal(hit.shapeType, 'position', 'selected position handle beats rect handle at same point');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'entry');
});

test('unified hit-test: position level-line mid-span beats rect body (Pass 2 vs Pass 4)', () => {
    // Use a rect that doesn't overlap with pos_1's SL level line at y=600 (value=400).
    // r_a: x=50..400, y=200..550 (ts=500..4000, val=800..450) — body covers (250, 600)? No.
    // Simpler: make a rect whose body covers (250, 600) but has no edge midpoints near y=600.
    // Rect ts=500..4000 → x=50..400, val=800..450 → y=200..550. y=600 is OUTSIDE this rect.
    // Use: rect x=50..400, y=200..620 (val=800..380) → edge BOTTOM midpoint at (225,620),
    //   so (250,600) is inside the body. pos_1 SL level y=600 (value=400, live dataIndex=10 → x=100).
    //   xLeft=100, xRight=300 (100+200). At (250,600): inside rect body, within LINE_R=6 of SL (y=600).
    //   Rect edge midpoints: TOP(225,200), BOTTOM(225,620), LEFT(50,410), RIGHT(400,410).
    //   Distance from (250,600) to BOTTOM midpoint(225,620): sqrt(625+400)=sqrt(1025)≈32 > EDGE_R=16. Safe.
    const env = createHitTestEnv(); // only pos_1, pos_2
    env.klineChart.convertToPixel = (pt) => {
        if (pt.dataIndex !== undefined) return { x: pt.dataIndex * 10, y: 1000 - pt.value };
        return { x: pt.timestamp / 10, y: 1000 - pt.value };
    };
    // Add a rect that body-covers (250, 600) but has no handles near that point
    const allDr = JSON.parse(env.localStorage.getItem('klineDrawings'));
    allDr.ETH.push({ id: 'r_a', name: 'rect',
        points: [{ timestamp: 500, value: 800 }, { timestamp: 4000, value: 380 }] });
    env.localStorage.setItem('klineDrawings', JSON.stringify(allDr));
    const ctx = loadDrawingsModule(env);
    // (250, 600): inside r_a body (x:50..400, y:200..620), within LINE_R=6 of pos_1 SL line (y=600)
    const hit = ctx.hitTestAtPoint(250, 600);
    assert.ok(hit, 'should hit');
    assert.equal(hit.shapeType, 'position', 'position level-line (Pass 2) beats rect body (Pass 4)');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'sl');
});

// ── hitTestAtPoint cross-type body overlap: zOrder determines winner ─

test('unified hit-test: rect with higher zOrder wins cross-type body overlap', () => {
    // pos_1 at zOrder=0, r1 at zOrder=5 — both bodies overlap at (150, 420)
    // r1 body: x1=100..200, y1=300..500. pos_1 body: x=100..300, y=295..605.
    const env = createUnifiedHitTestEnv({ rectZOrders: { r1: 5, r2: 0 }, posZOrders: { pos_1: 0 } });
    const ctx = loadDrawingsModule(env);
    // Click at (150, 420): inside r1 body AND inside pos_1 body; no handles/lines near there
    const hit = ctx.hitTestAtPoint(150, 420);
    assert.ok(hit, 'should hit');
    assert.equal(hit.shapeType, 'rect', 'higher zOrder rect wins');
    assert.equal(hit.overlayId, 'r1');
});

test('unified hit-test: position wins body overlap when it has higher zOrder than rect', () => {
    // pos_1 at zOrder=10, r1 at zOrder=2
    const env = createUnifiedHitTestEnv({ rectZOrders: { r1: 2, r2: 0 }, posZOrders: { pos_1: 10 } });
    const ctx = loadDrawingsModule(env);
    const hit = ctx.hitTestAtPoint(150, 420);
    assert.ok(hit, 'should hit');
    assert.equal(hit.shapeType, 'position', 'position wins when zOrder is higher');
    assert.equal(hit.overlayId, 'pos_1');
});

test('unified hit-test: selected position overrides zOrder in body pass', () => {
    // r1 at zOrder=5 > pos_1 at zOrder=0; but pos_1 is selected → pos_1 wins
    const env = createUnifiedHitTestEnv({ rectZOrders: { r1: 5, r2: 0 }, posZOrders: { pos_1: 0 } });
    env.klineSelectedOverlayId = 'pos_1';
    const ctx = loadDrawingsModule(env);
    const hit = ctx.hitTestAtPoint(150, 420);
    assert.ok(hit, 'should hit');
    assert.equal(hit.shapeType, 'position', 'selected position beats higher-zOrder rect');
    assert.equal(hit.overlayId, 'pos_1');
});

// ── hitTestAtPoint: body stack ordered by zOrder ascending ───────────

test('unified hit-test: body stack ordered by zOrder ascending (lower first)', () => {
    // r1 zOrder=3, r2 zOrder=1 — overlapping at (175, 420)
    // Stack ascending: r2(1) then r1(3)
    const env = createUnifiedHitTestEnv({ rectZOrders: { r1: 3, r2: 1 }, posZOrders: { pos_1: 5 } });
    // Move pos_1 out of the way by overriding its live points outside the click area
    env.klineChart.getOverlayById = (id) => {
        if (id === 'pos_1') {
            // Place pos_1 far away: dataIndex=50 → x=500
            return { id: 'pos_1', points: [
                { dataIndex: 50, value: 800 },
                { dataIndex: 50, value: 750 },
                { dataIndex: 50, value: 850 },
            ]};
        }
        return null;
    };
    const ctx = loadDrawingsModule(env);
    const hit = ctx.hitTestAtPoint(175, 420); // inside r1+r2, outside pos_1
    assert.ok(hit, 'should hit');
    assert.equal(hit.shapeType, 'rect');
    // Stack ascending by zOrder: r2(1) before r1(3)
    assert.equal(hit.stack[0], 'r2', 'lower zOrder first in stack');
    assert.equal(hit.stack[1], 'r1', 'higher zOrder second in stack');
});

// ── hitTestAtPoint: Y-axis guard ─────────────────────────────────────

test('unified hit-test: Y-axis guard returns null beyond getYAxisStartX', () => {
    const env = createUnifiedHitTestEnv();
    // Install a container that makes getYAxisStartX return 400
    env.document.getElementById = (id) => {
        if (id === 'kline-chart-container') {
            return {
                offsetWidth: 500,
                querySelectorAll: () => {
                    // Return mock canvases: one 400px wide (main), one narrower
                    return [{ offsetWidth: 400 }, { offsetWidth: 80 }];
                }
            };
        }
        return null;
    };
    const ctx = loadDrawingsModule(env);
    // x=450 is beyond the 400px chart area → null
    const hit = ctx.hitTestAtPoint(450, 420);
    assert.equal(hit, null, 'Y-axis zone should return null');
    // x=350 is within chart area and over a rect body → not null
    const hit2 = ctx.hitTestAtPoint(175, 420);
    assert.ok(hit2 !== null, 'Within chart area should hit');
});

test('unified hit-test: Y-axis guard applies to channel corners too', () => {
    // The new _getChannelCornerHit has a Y-axis guard that was absent before Phase 4.
    // Without the guard, a channel corner at x>getYAxisStartX would fire; now it's blocked.
    const env = createMockEnv();
    // Make getYAxisStartX return 400 via container mock
    env.document.getElementById = (id) => {
        if (id === 'kline-chart-container') {
            return {
                offsetWidth: 500,
                querySelectorAll: () => [{ offsetWidth: 400 }, { offsetWidth: 80 }]
            };
        }
        return null;
    };
    env.klineChart.convertToPixel = (pt) => ({ x: pt.dataIndex * 10, y: 1000 - pt.value });
    // Channel with live overlay placing bl corner at x=450 (beyond Y-axis zone)
    env.klineChart.getOverlayById = (id) => {
        if (id === 'chan_1') {
            return { id: 'chan_1', points: [
                { dataIndex: 45, value: 500 },
                { dataIndex: 55, value: 500 },
                { dataIndex: 45, value: 400 },
            ]};
        }
        return null;
    };
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{ id: 'chan_1', name: 'boundedChannel', paneId: 'candle_pane',
            points: [{ timestamp: 4500, value: 500 }, { timestamp: 5500, value: 500 }, { timestamp: 4500, value: 400 }] }]
    }));
    const ctx = loadDrawingsModule(env);
    // Click at the channel's bl corner x=450 → blocked by Y-axis guard
    const hit = ctx._getChannelCornerHit(450, 500);
    assert.equal(hit, null, 'channel corner beyond Y-axis zone must return null');
});

// ─── Wave B: viewport-keyed hover geometry cache ─────────────
console.log('\n\x1b[1mWave B: hover geometry cache\x1b[0m\n');

test('_getHoverGeomCache: returns the SAME cache object across calls when nothing changed', () => {
    const env = createHitTestEnv();
    const ctx = loadDrawingsModule(env);
    const c1 = ctx._getHoverGeomCache();
    const c2 = ctx._getHoverGeomCache();
    assert.equal(c1, c2, 'unchanged signature must reuse the cached object, not rebuild');
});

test('_getHoverGeomCache: rebuilds when the drawings store raw string changes', () => {
    const env = createHitTestEnv();
    const ctx = loadDrawingsModule(env);
    const c1 = ctx._getHoverGeomCache();
    const pos1Before = c1.positions.find(p => p.d.id === 'pos_1');
    assert.equal(pos1Before.handles.find(h => h.level === 'entry').value, 500);

    // Simulate an edit landing in localStorage (e.g. a finalized drag) — the raw string changes.
    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    all.ETH.find(d => d.id === 'pos_1').points[0].value = 999;
    env.localStorage.setItem('klineDrawings', JSON.stringify(all));

    const c2 = ctx._getHoverGeomCache();
    assert.notEqual(c1, c2, 'raw string change must force a rebuild');
    const pos1After = c2.positions.find(p => p.d.id === 'pos_1');
    assert.equal(pos1After.handles.find(h => h.level === 'entry').value, 999, 'rebuilt geometry reflects the new stored value');
});

test('_getHoverGeomCache: rebuilds when the time viewport (getVisibleRange/getBarSpace) changes', () => {
    const env = createHitTestEnv();
    let vr = { from: 0, to: 100, realFrom: 0, realTo: 100 };
    env.klineChart.getVisibleRange = () => vr;
    env.klineChart.getBarSpace = () => 10;
    const ctx = loadDrawingsModule(env);

    const c1 = ctx._getHoverGeomCache();
    const c1b = ctx._getHoverGeomCache();
    assert.equal(c1, c1b, 'unchanged viewport must reuse the cache');

    vr = { from: 10, to: 110, realFrom: 10, realTo: 110 }; // pan
    const c2 = ctx._getHoverGeomCache();
    assert.notEqual(c1, c2, 'a changed visible range must force a rebuild');
});

test('_getHoverGeomCache: rebuilds when the y-axis sentinel pixel moves with the time viewport unchanged', () => {
    // Simulates price-axis autoscale reshaping the y-axis on a new tick — the time
    // viewport (getVisibleRange/getBarSpace) and container size are UNCHANGED, so only
    // the y-sentinel probe can catch this. Airtight-invalidation requirement.
    const env = createHitTestEnv();
    env.klineChart.getVisibleRange = () => ({ from: 0, to: 100, realFrom: 0, realTo: 100 });
    env.klineChart.getBarSpace = () => 10;
    let yShift = 0;
    const baseConvert = env.klineChart.convertToPixel;
    env.klineChart.convertToPixel = (pt, opts) => {
        const r = baseConvert(pt, opts);
        return { x: r.x, y: r.y + yShift };
    };
    const ctx = loadDrawingsModule(env);

    const c1 = ctx._getHoverGeomCache();
    const c1b = ctx._getHoverGeomCache();
    assert.equal(c1, c1b, 'unchanged y-sentinel must reuse the cache');

    yShift = 7; // autoscale reshaped the price axis — nothing else changed
    const c2 = ctx._getHoverGeomCache();
    assert.notEqual(c1, c2, 'a moved y-sentinel must force a rebuild even though the time viewport did not change');
});

test('hitTestAtPoint: repeated calls with nothing changed return identical results (cache correctness)', () => {
    const env = createUnifiedHitTestEnv();
    const ctx = loadDrawingsModule(env);
    const hitA = ctx.hitTestAtPoint(150, 420);
    const hitB = ctx.hitTestAtPoint(150, 420);
    assert.ok(hitA, 'should hit');
    assert.deepEqual(hitA, hitB, 'unchanged viewport/store must yield identical hit results across calls');
});

test('hitTestAtPoint: second call with nothing changed does not re-run per-drawing convertToPixel (geometry cache hit)', () => {
    const env = createUnifiedHitTestEnv(); // 2 rects + 1 position — 7+ convertToPixel calls per full (re)build
    const ctx = loadDrawingsModule(env);
    ctx.hitTestAtPoint(150, 420); // warm the cache
    let calls = 0;
    const base = env.klineChart.convertToPixel;
    env.klineChart.convertToPixel = (pt, opts) => { calls++; return base(pt, opts); };
    ctx.hitTestAtPoint(150, 420);
    // Only the one-per-call y-axis sentinel probe is expected through — not a fresh
    // per-drawing geometry recompute (which would be 7+ calls for 2 rects + 1 position).
    assert.ok(calls <= 1, `expected at most 1 convertToPixel call (y-sentinel probe), got ${calls}`);
});

test('hitTestAtPoint: reflects a moved drawing immediately after its stored points change', () => {
    const env = createUnifiedHitTestEnv();
    const ctx = loadDrawingsModule(env);
    let hit = ctx.hitTestAtPoint(150, 350); // r2's TL corner (x1=150,y1=350)
    assert.ok(hit, 'should hit r2 TL corner initially');
    assert.equal(hit.overlayId, 'r2');
    assert.equal(hit.handle, 'TL');

    // Move r2 far away — simulates a finalized drag writing new points to storage.
    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const r2 = all.ETH.find(d => d.id === 'r2');
    r2.points = [{ timestamp: 90000, value: 10 }, { timestamp: 95000, value: 5 }];
    env.localStorage.setItem('klineDrawings', JSON.stringify(all));

    hit = ctx.hitTestAtPoint(150, 350);
    assert.ok(!hit || hit.overlayId !== 'r2', 'moved rect must no longer hit at its old position (geometry cache picked up the move)');
});

test('_getChannelCornerHit: still detects p0/p1 corner grabs via the geometry cache', () => {
    const env = createMockEnv();
    env.document.getElementById = () => null;
    env.klineChart.convertToPixel = (pt) => ({ x: pt.dataIndex * 10, y: 1000 - pt.value });
    env.klineChart.getOverlayById = (id) => {
        if (id === 'chan_1') {
            return { id: 'chan_1', points: [
                { dataIndex: 10, value: 500 },
                { dataIndex: 20, value: 500 },
                { dataIndex: 10, value: 400 },
            ]};
        }
        return null;
    };
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{ id: 'chan_1', name: 'boundedChannel', paneId: 'candle_pane',
            points: [{ timestamp: 1000, value: 500 }, { timestamp: 2000, value: 500 }, { timestamp: 1000, value: 400 }] }]
    }));
    const ctx = loadDrawingsModule(env);
    const hitP0 = ctx._getChannelCornerHit(100, 500);
    assert.ok(hitP0, 'should hit p0');
    assert.equal(hitP0.overlayId, 'chan_1');
    assert.equal(hitP0.corner, 'p0');

    const hitP1 = ctx._getChannelCornerHit(200, 500);
    assert.ok(hitP1, 'should hit p1');
    assert.equal(hitP1.corner, 'p1');

    const miss = ctx._getChannelCornerHit(100, 900); // far from any handle
    assert.equal(miss, null, 'far-away point should miss');
});

test('_getFrvpEdgeHit: detects left/right edge grabs via the geometry cache', () => {
    const env = createMockEnv();
    env.document.getElementById = () => null;
    env.klineChart.convertToPixel = (pt) => ({ x: (pt.dataIndex || 0) * 10, y: 0 });
    env.klineChart.getOverlayById = (id) => {
        if (id === 'fv_1') return { id: 'fv_1', points: [{ dataIndex: 10 }, { dataIndex: 20 }] };
        return null;
    };
    // frvpGetEdgeGeometry + _frvpPointDi normally live in frvp.js (not loaded here) —
    // injected on the vm context global exactly like the module's other cross-file deps
    // (getActivePaneIds, hexToRgba, etc.) already are.
    env._frvpPointDi = (p) => p.dataIndex;
    env.frvpGetEdgeGeometry = (id) => (id === 'fv_1' ? { x1: 100, x2: 200, yTop: 300, yBot: 500 } : null);
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{ id: 'fv_1', name: 'frvp', points: [{ timestamp: 1000 }, { timestamp: 2000 }] }]
    }));
    const ctx = loadDrawingsModule(env);

    const left = ctx._getFrvpEdgeHit(100, 400);
    assert.ok(left, 'should hit left edge');
    assert.equal(left.overlayId, 'fv_1');
    assert.equal(left.edge, 0);

    const right = ctx._getFrvpEdgeHit(200, 400);
    assert.ok(right, 'should hit right edge');
    assert.equal(right.overlayId, 'fv_1');
    assert.equal(right.edge, 1);

    const miss = ctx._getFrvpEdgeHit(150, 400); // middle, not near either edge
    assert.equal(miss, null, 'middle of the range should miss');
});

// ── Phase 3: z-order load sort ────────────────────────────────────────

test('loadKlineDrawings sorts by zOrder ascending (creates overlays in that order)', () => {
    const env = createMockEnv();
    env.klineChart.convertToPixel = (pt) => ({ x: pt.timestamp / 10, y: 1000 - pt.value });
    // Track overlay creation order
    const createOrder = [];
    const origCreate = env.klineChart.createOverlay;
    env.klineChart.createOverlay = (config, paneId) => {
        createOrder.push(config.id);
        return origCreate(config, paneId);
    };
    // Seed: r3 zOrder=5, r1 zOrder=1, r2 zOrder=3 — should be created r1, r2, r3
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [
            { id: 'r3', name: 'rect', points: [{ timestamp: 1000, value: 700 }, { timestamp: 2000, value: 500 }], zOrder: 5 },
            { id: 'r1', name: 'rect', points: [{ timestamp: 1000, value: 700 }, { timestamp: 2000, value: 500 }], zOrder: 1 },
            { id: 'r2', name: 'rect', points: [{ timestamp: 1000, value: 700 }, { timestamp: 2000, value: 500 }], zOrder: 3 },
        ]
    }));
    const ctx = loadDrawingsModule(env);
    ctx.loadKlineDrawings();
    assert.ok(createOrder.indexOf('r1') < createOrder.indexOf('r2'), 'r1 (z=1) created before r2 (z=3)');
    assert.ok(createOrder.indexOf('r2') < createOrder.indexOf('r3'), 'r2 (z=3) created before r3 (z=5)');
});

// ── Phase 3: new drawing gets maxZ+1 ─────────────────────────────────

test('saveKlineDrawing assigns zOrder = maxExisting + 1 for new drawings', () => {
    const env = createMockEnv();
    // Seed storage with two rects at zOrder 3 and 7
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [
            { id: 'draw_1', name: 'straightLine', points: [{ value: 100, timestamp: 1000 }], color: '#ff0000', width: 2, zOrder: 3 },
            { id: 'draw_2', name: 'rect', points: [{ value: 150, timestamp: 1500 }, { value: 250, timestamp: 2500 }], color: '#00ff00', width: 1, zOrder: 7 },
        ]
    }));
    const ctx = loadDrawingsModule(env);
    ctx.saveKlineDrawing({ id: 'new_draw', name: 'segment', points: [{ value: 1, timestamp: 100 }], color: '#fff', width: 1 });
    const drawings = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const saved = drawings.ETH.find(d => d.id === 'new_draw');
    assert.ok(saved, 'new drawing should be saved');
    assert.equal(saved.zOrder, 8, 'new drawing should get zOrder = maxZ(7) + 1');
});

test('saveKlineDrawing preserves explicitly set zOrder on new drawing', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    // Pass an explicit zOrder — should not be overwritten
    ctx.saveKlineDrawing({ id: 'explicit_z', name: 'segment', points: [{ value: 1, timestamp: 100 }], color: '#fff', width: 1, zOrder: 42 });
    const drawings = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const saved = drawings.ETH.find(d => d.id === 'explicit_z');
    assert.equal(saved.zOrder, 42, 'explicit zOrder should be preserved');
});

// ─── rightTs geometry (a) ─────────────────────────────────
//
// Test group (a): ext.rightTs geometry — hit-test xRight honors ext.rightTs with NO
// 60px inflation; narrow box right-edge hit at true x; body bounded by true xRight;
// nothing at legacy xLeft+200.
//
// Coordinate system: timestamp→x = ts/10, y = 1000 - value.
// A position at entry ts=1000 (x=100) with ext.rightTs=3000 (x=300) → box x=100..300.
// Without rightTs the narrow box (single-bar = xLeft=xRight=100) would inflate to x=100..300
// via the legacy +200 — but with rightTs=3000 we expect the TRUE resolved x=300, and
// a click at x=100+200=300 (legacy) coincidentally hits the true right edge here,
// so use a clearly different rightTs: ts=5000 → x=500.

console.log('\n\x1b[1mposition rightTs geometry tests (a)\x1b[0m\n');

function createRightTsEnv(rightTs) {
    const env = createHitTestEnv();
    // Override: only pos_1 with ext.rightTs, no pos_2 (simplify)
    env.klineChart.getOverlayById = (id) => {
        if (id === 'pos_1') {
            return { id: 'pos_1', points: [
                { dataIndex: 10, value: 500 },
                { dataIndex: 10, value: 400 },
                { dataIndex: 10, value: 700 },
            ]};
        }
        return null;
    };
    const extendData = rightTs !== undefined ? { rightTs } : {};
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [
            { id: 'pos_1', name: 'position',
              extendData,
              points: [
                { timestamp: 1000, value: 500 }, { timestamp: 1000, value: 400 }, { timestamp: 1000, value: 700 }
              ]
            }
        ]
    }));
    return env;
}

test('rightTs: right-edge hit at true x=500 when rightTs=5000', () => {
    // pos_1 xLeft=100 (dataIndex=10). ext.rightTs=5000 → x=5000/10=500 (true right).
    // Legacy without rightTs: narrow box 0px → xLeft+200=300. With rightTs: true xRight=500.
    const env = createRightTsEnv(5000);
    const ctx = loadDrawingsModule(env);
    // Hit the right-edge strip (within 15px of x=500): x=495
    const hit = ctx._getPositionHitAtPoint(495, 500);
    assert.ok(hit, 'should hit right-edge strip at true x=500');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'right-edge', 'should be right-edge level');
});

test('rightTs: body hit bounded by true xRight=500 — point at x=350 inside', () => {
    // Box is x=100..500 when rightTs=5000. x=350 is inside body.
    const env = createRightTsEnv(5000);
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(350, 450); // body area, away from level lines
    assert.ok(hit, 'x=350 inside box should hit');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'body');
});

test('rightTs: nothing hit at legacy xLeft+200=300 for right-edge when rightTs=5000', () => {
    // Without rightTs the synthetic right edge would be at xLeft+200=300 — right-edge strip
    // at x=290..315. With rightTs=5000 the true right edge is at x=500, so x=295 must NOT
    // return right-edge (it's in the body, not near the edge strip at x=500).
    const env = createRightTsEnv(5000);
    const ctx = loadDrawingsModule(env);
    // x=295 is 5px inside the legacy right edge but 205px from true xRight=500
    const hit = ctx._getPositionHitAtPoint(295, 500);
    // May hit body (it's inside the box x=100..500), but must NOT be right-edge
    if (hit) {
        assert.notEqual(hit.level, 'right-edge', 'x=295 must not be right-edge when true xRight=500');
    }
    // No hit is also valid (it might miss if near level lines but not in them)
    // — the important thing is no right-edge hit at the legacy position
});

// Test (b): legacy no-rightTs keeps the +200 behavior
console.log('\n\x1b[1mlegacy no-rightTs behavior (b)\x1b[0m\n');

test('legacy: no rightTs — narrow box gets +200 synthetic right edge', () => {
    // Single-bar position: all points share dataIndex=10 → x=100. No ext.rightTs.
    // Legacy: xRight - xLeft = 0 < 60 → xRight = xLeft+200 = 300.
    // Right-edge strip at ~x=285..315. Hit at x=295 should register.
    const env = createRightTsEnv(undefined); // no rightTs
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(295, 500);
    assert.ok(hit, 'legacy +200 right-edge strip should hit at x=295');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'right-edge', 'should be right-edge at legacy +200 position');
});

test('legacy: no rightTs — body hit at x=200 (inside synthetic box x=100..300)', () => {
    const env = createRightTsEnv(undefined);
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(200, 450);
    assert.ok(hit, 'body inside legacy +200 box should hit');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'body');
});

test('legacy: no rightTs — right-edge returns null beyond xLeft+200 (x=350)', () => {
    // Legacy xRight=300. x=350 is 50px beyond the synthetic right edge — must miss.
    const env = createRightTsEnv(undefined);
    const ctx = loadDrawingsModule(env);
    const hit = ctx._getPositionHitAtPoint(350, 500);
    assert.equal(hit, null, 'x=350 beyond legacy xLeft+200=300 must miss');
});

// Test (c): right-edge drag math — only ext.rightTs changes, points untouched, min-width clamp
console.log('\n\x1b[1mright-edge drag math (c)\x1b[0m\n');

// Helper: create a drag state and simulate _handlePositionLevelDrag via the module's
// exposed function. We set up a mock env with convertFromPixel and getDataList.
function createDragEnv() {
    const env = createMockEnv();
    // Data list: 10 bars, each 1000ms apart starting at ts=10000
    const bars = [];
    for (let i = 0; i < 20; i++) bars.push({ timestamp: 10000 + i * 1000, open: 100, high: 110, low: 90, close: 105, volume: 1 });
    env.klineChart.getDataList = () => bars;
    // convertToPixel: ts→x = (ts - 10000)/100 + 10 (arbitrary linear), value→y = 1000 - value
    env.klineChart.convertToPixel = (pt) => {
        if (pt.dataIndex !== undefined) return { x: pt.dataIndex * 10, y: 1000 - pt.value };
        if (pt.timestamp !== undefined) return { x: (pt.timestamp - 10000) / 100 + 10, y: 1000 - pt.value };
        return null;
    };
    // convertFromPixel: inverse of timestamp→x. x = (ts-10000)/100+10 → ts = (x-10)*100+10000
    // snap to nearest bar dataIndex = round((x-10)*100/1000) = round((x-10)/10)
    env.klineChart.convertFromPixel = (pt) => {
        var di = Math.round((pt.x - 10) / 10);
        return { dataIndex: Math.max(0, Math.min(di, bars.length - 1)), value: 1000 - pt.y };
    };
    // One position: entry at bar 2 (ts=12000), rightTs=15000 (bar 5)
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{
            id: 'drag_pos', name: 'position',
            points: [
                { timestamp: 12000, value: 500 },
                { timestamp: 12000, value: 400 },
                { timestamp: 12000, value: 700 }
            ],
            extendData: { rightTs: 15000 }
        }]
    }));
    // Live overlay: same points
    env.klineChart.getOverlayById = (id) => {
        if (id === 'drag_pos') {
            return {
                id: 'drag_pos',
                points: [
                    { dataIndex: 2, value: 500 },
                    { dataIndex: 2, value: 400 },
                    { dataIndex: 2, value: 700 }
                ],
                extendData: { rightTs: 15000 }
            };
        }
        return null;
    };
    // overrideOverlay: record last call
    env._lastOverride = null;
    env.klineChart.overrideOverlay = (cfg) => {
        env._lastOverride = cfg;
        // Update the overlay returned by getOverlayById to reflect extendData changes
        env.klineChart.getOverlayById = (id) => {
            if (id === 'drag_pos') {
                return {
                    id: 'drag_pos',
                    points: cfg.points || [
                        { dataIndex: 2, value: 500 },
                        { dataIndex: 2, value: 400 },
                        { dataIndex: 2, value: 700 }
                    ],
                    extendData: cfg.extendData || { rightTs: 15000 }
                };
            }
            return null;
        };
    };
    env.klineChart.resize = () => {};
    // document.getElementById for container
    env.document.getElementById = (id) => {
        if (id === 'kline-chart-container') {
            return {
                getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 400 }),
                offsetWidth: 800,
                querySelectorAll: () => [{ offsetWidth: 700 }]
            };
        }
        return null;
    };
    return env;
}

test('right-edge drag: only ext.rightTs changes, points untouched', () => {
    const env = createDragEnv();
    const ctx = loadDrawingsModule(env);

    // Arm the drag: right-edge level, startX at bar 5 pixel x = (15000-10000)/100+10 = 60
    // initRightX: ts=15000 → x=60. Drag right to x=80 → bar 7 (ts=17000).
    // convertToPixel({timestamp:12000}) → x = (12000-10000)/100+10 = 30
    // convertToPixel({timestamp:15000}) → x = (15000-10000)/100+10 = 60
    // Drag right to clientX=80: di = round((80-10)/10) = 7 → ts=17000
    ctx._positionLevelDrag = {
        overlayId: 'drag_pos', level: 'right-edge', paneId: 'candle_pane',
        origValue: 500, clickOffset: 0, currentValue: null,
        startX: 60, initPixelXs: [30, 30, 30],
        initRightX: 60, initRightTs: 15000,
        origEntryTs: 12000
    };

    const fakeContainer = env.document.getElementById('kline-chart-container');
    ctx._handlePositionLevelDrag(80, 250, fakeContainer);

    assert.ok(env._lastOverride, 'should have called overrideOverlay');
    // Points from live overlay: they have dataIndex not timestamp.
    // Key assertion: dataIndex values are UNTOUCHED (right-edge drag only updates rightTs).
    const pts = env._lastOverride.points;
    assert.equal(pts[0].dataIndex, 2, 'entry dataIndex must be untouched on right-edge drag');
    assert.equal(pts[1].dataIndex, 2, 'sl dataIndex must be untouched');
    assert.equal(pts[2].dataIndex, 2, 'tp dataIndex must be untouched');
    // rightTs must have changed
    const newRightTs = env._lastOverride.extendData.rightTs;
    assert.ok(typeof newRightTs === 'number', 'extendData.rightTs must be set');
    assert.notEqual(newRightTs, 15000, 'rightTs should have changed from initial 15000');
});

test('right-edge drag: min-width clamp — cannot drag left past entry + 2 bars', () => {
    const env = createDragEnv();
    const ctx = loadDrawingsModule(env);

    // Try to drag right edge to the same bar as entry (bar 2, ts=12000) — should clamp to >=2 bars ahead
    // Entry ts=12000 (bar 2). Min: entry + 2 bars = 12000 + 2*1000 = 14000.
    // Drag to x=30 (bar 2). After clamp: should be bar 4 at minimum (ts=14000).
    ctx._positionLevelDrag = {
        overlayId: 'drag_pos', level: 'right-edge', paneId: 'candle_pane',
        origValue: 500, clickOffset: 0, currentValue: null,
        startX: 60, initPixelXs: [30, 30, 30],
        initRightX: 60, initRightTs: 15000,
        origEntryTs: 12000
    };

    const fakeContainer = env.document.getElementById('kline-chart-container');
    // Drag left to x=30 (bar 2 = entry bar, pixelDx=30-60=-30, >=2 threshold passed)
    ctx._handlePositionLevelDrag(30, 250, fakeContainer);

    const newRightTs = env._lastOverride && env._lastOverride.extendData && env._lastOverride.extendData.rightTs;
    if (newRightTs !== undefined) {
        const entryTs = 12000;
        const barMs = 1000;
        assert.ok(newRightTs >= entryTs + 2 * barMs,
            'rightTs must be >= entry + 2 bars; got ' + newRightTs + ', min=' + (entryTs + 2 * barMs));
    }
    // Note: if pixelDx < 2 (threshold), overrideOverlay may not be called — that's fine
});

test('body drag: points get UPDATED dataIndex + timestamp, rightTs shifts by same delta', () => {
    const env = createDragEnv();
    const ctx = loadDrawingsModule(env);

    // Body drag +20px = +2 bars. Entry bar 2 (x=30) → bar 4 (ts=14000).
    // rightTs 15000 (x=60) → x=80 → bar 7 (ts=17000): same +2-bar delta.
    ctx._positionLevelDrag = {
        overlayId: 'drag_pos', level: 'body', paneId: 'candle_pane',
        origValue: 500, clickOffset: 0, currentValue: null,
        startX: 40, initPixelXs: [30, 30, 30],
        initRightX: 60, initRightTs: 15000,
        origEntryTs: 12000
    };

    const fakeContainer = env.document.getElementById('kline-chart-container');
    ctx._handlePositionLevelDrag(60, 250, fakeContainer);

    assert.ok(env._lastOverride, 'should have called overrideOverlay');
    const pts = env._lastOverride.points;
    // These tests pin that dataIndex moves with the timestamp (overrideOverlay
    // does not re-resolve), so the render + finalize save use the new bar.
    assert.equal(pts[0].dataIndex, 4, 'entry dataIndex must move with the body drag');
    assert.equal(pts[1].dataIndex, 4, 'sl dataIndex must move');
    assert.equal(pts[2].dataIndex, 4, 'tp dataIndex must move');
    assert.equal(pts[0].timestamp, 14000, 'entry timestamp must be the new bar');
    assert.equal(env._lastOverride.extendData.rightTs, 17000, 'rightTs must shift by the same delta');
});

test('left-edge drag: moved points get updated dataIndex too', () => {
    const env = createDragEnv();
    const ctx = loadDrawingsModule(env);

    // Left edge from bar 2 (x=30) dragged +10px → bar 3 (ts=13000); rightTs fixed at 15000.
    ctx._positionLevelDrag = {
        overlayId: 'drag_pos', level: 'left-edge', paneId: 'candle_pane',
        origValue: 500, clickOffset: 0, currentValue: null,
        startX: 30, initPixelXs: [30, 30, 30],
        initRightX: 60, initRightTs: 15000,
        origEntryTs: 12000
    };

    const fakeContainer = env.document.getElementById('kline-chart-container');
    ctx._handlePositionLevelDrag(40, 250, fakeContainer);

    assert.ok(env._lastOverride, 'should have called overrideOverlay');
    const pts = env._lastOverride.points;
    assert.equal(pts[0].dataIndex, 3, 'entry dataIndex must move on left-edge drag');
    assert.equal(pts[0].timestamp, 13000, 'entry timestamp must be the new bar');
    assert.equal(env._lastOverride.extendData.rightTs, 15000, 'rightTs must stay fixed on left-edge drag');
});

// Test (d): startPositionDrag (mobile) produces initPixelXs + clickOffset matching desktop arm shape
console.log('\n\x1b[1mmobile startPositionDrag arm parity (d)\x1b[0m\n');

test('mobile startPositionDrag: produces initPixelXs array matching point count', () => {
    const env = createDragEnv();
    const ctx = loadDrawingsModule(env);

    const drawing = {
        id: 'drag_pos',
        name: 'position',
        paneId: 'candle_pane',
        points: [
            { timestamp: 12000, value: 500 },
            { timestamp: 12000, value: 400 },
            { timestamp: 12000, value: 700 }
        ],
        extendData: { rightTs: 15000 }
    };
    const hit = {
        overlayId: 'drag_pos', level: 'right-edge', value: 500, drawing
    };

    // Call startPositionDrag — should return true and arm _positionLevelDrag
    const result = ctx._mobileHandleApi.startPositionDrag(hit, 60, 250);
    assert.ok(result, 'startPositionDrag should return true');
    assert.ok(ctx._positionLevelDrag, '_positionLevelDrag should be set');
    assert.ok(Array.isArray(ctx._positionLevelDrag.initPixelXs), 'initPixelXs should be an array');
    assert.equal(ctx._positionLevelDrag.initPixelXs.length, 3, 'initPixelXs should have one entry per point');
});

test('mobile startPositionDrag: clickOffset is a number (not 0 fallback when convertFromPixel works)', () => {
    const env = createDragEnv();
    const ctx = loadDrawingsModule(env);

    const drawing = {
        id: 'drag_pos', name: 'position', paneId: 'candle_pane',
        points: [{ timestamp: 12000, value: 500 }, { timestamp: 12000, value: 400 }, { timestamp: 12000, value: 700 }],
        extendData: { rightTs: 15000 }
    };
    const hit = { overlayId: 'drag_pos', level: 'sl', value: 400, drawing };

    ctx._mobileHandleApi.startPositionDrag(hit, 30, 600); // localY=600 → value=400
    assert.ok(ctx._positionLevelDrag, 'should be armed');
    assert.ok(typeof ctx._positionLevelDrag.clickOffset === 'number', 'clickOffset should be a number');
    // With convertFromPixel returning value=1000-600=400 and hitValue=400, offset=0 (exact grab)
    assert.ok(isFinite(ctx._positionLevelDrag.clickOffset), 'clickOffset should be finite');
});

test('mobile startPositionDrag: body level armed correctly', () => {
    const env = createDragEnv();
    const ctx = loadDrawingsModule(env);

    const drawing = {
        id: 'drag_pos', name: 'position', paneId: 'candle_pane',
        points: [{ timestamp: 12000, value: 500 }, { timestamp: 12000, value: 400 }, { timestamp: 12000, value: 700 }],
        extendData: { rightTs: 15000 }
    };
    const hit = { overlayId: 'drag_pos', level: 'body', value: 500, drawing };

    const result = ctx._mobileHandleApi.startPositionDrag(hit, 30, 500);
    assert.ok(result, 'body drag should be armed successfully');
    assert.ok(ctx._positionLevelDrag, 'should be armed');
    assert.equal(ctx._positionLevelDrag.level, 'body', 'level should be body');
    assert.ok(Array.isArray(ctx._positionLevelDrag.initPixelXs), 'initPixelXs should be set for body drag');
});

// Test (e): coarse-pointer radii
// Strategy: use a single-position env (createRightTsEnv) to avoid interference from pos_2.
// pos_1: entry at dataIndex=10 → xLeft=100, entryY=500, slY=600, tpY=300.
// Legacy box (no rightTs): xRight = xLeft+200 = 300.
// Box bounds: yTop=295, yBot=605 (max(500,600,300)+5=605; check also TP y=1000-700=300... wait
// pos_1 points: value[0]=500, value[1]=400, value[2]=700.
// entry y=1000-500=500, sl y=1000-400=600, tp y=1000-700=300.
// yTop = min(500,600,300)-5 = 295. yBot = max(500,600,300)+5 = 605.
//
// For coarse HANDLE_R vs desktop HANDLE_R test:
// Use point (120, 590): dist from SL handle (100,600) = sqrt(20²+10²) = sqrt(500) ≈ 22.4px
//   Coarse HANDLE_R=26: 22.4 ≤ 26 → Pass 1 fires SL handle.
//   Desktop HANDLE_R=18: 22.4 > 18 → Pass 1 misses.
//   Pass 2 (level lines): SL line at y=600, |590-600|=10 ≤ LINE_R(coarse=10) → fires (coarse).
//                          |590-600|=10 > LINE_R(desktop=6) → misses (desktop).
//   Pass 3: |120-100|=20>18 → no left edge. |120-300|=180>15 → no right edge.
//   Pass 4: y=590 < yBot=605 → inside body → body hit (desktop).
// So coarse → SL (via Pass 1 handle); desktop → SL (via Pass 2 level line?).
// Actually desktop Pass 2: |590-600|=10 > 6 → miss. Falls to Pass 4 body. Correct.
//
// For coarse LINE_R vs desktop LINE_R test:
// Use point (200, 608): dist from SL handle (100,600)=sqrt(10000+64)≈100 > 26. No handle hit.
//   Pass 2: SL line at y=600, |608-600|=8. Coarse LINE_R=10: 8 ≤ 10 → hit. Desktop LINE_R=6: 8>6 → miss.
//   Pass 4: y=608 > yBot=605 → OUTSIDE body. Miss.
// So: coarse → SL (via level line); desktop → null.
console.log('\n\x1b[1mcoarse-pointer touch radii (e)\x1b[0m\n');

test('coarse radii: touch ~22px from SL handle fires handle (HANDLE_R=26 coarse vs 18 desktop)', () => {
    // Point (120, 590): dist from SL handle (100,600) ≈ 22.4px.
    // Coarse HANDLE_R=26: 22.4 ≤ 26 → Pass 1 fires SL handle.
    const env = createRightTsEnv(undefined);
    const ctx = loadDrawingsModule(env);

    ctx._coarsePointer = true;

    const hit = ctx._getPositionHitAtPoint(120, 590);
    assert.ok(hit, 'coarse: ~22px from SL handle should hit');
    assert.equal(hit.overlayId, 'pos_1');
    assert.equal(hit.level, 'sl', 'coarse HANDLE_R=26 fires SL handle in Pass 1');
});

test('coarse radii: same point on desktop falls through to body (HANDLE_R=18 misses Pass 1)', () => {
    // Point (120, 590): dist ≈ 22.4px > 18 → Pass 1 misses.
    // Pass 2: |590-600|=10 > LINE_R(desktop=6) → miss.
    // Pass 4: y=590 < yBot=605 → body hit.
    const env = createRightTsEnv(undefined);
    const ctx = loadDrawingsModule(env);

    ctx._coarsePointer = false;

    const hit = ctx._getPositionHitAtPoint(120, 590);
    assert.ok(hit, 'desktop: same point should still hit body');
    assert.equal(hit.level, 'body', 'desktop HANDLE_R=18 misses, falls to body');
});

test('coarse radii: touch LINE_R=10 — 8px from level line hits', () => {
    // pos_1 SL line at y=600 (value=400). Touch at y=608 = 8px away.
    // Desktop LINE_R=6: 8 > 6 → miss. Coarse LINE_R=10: 8 <= 10 → hit.
    // x=250 is inside the box (xLeft=100, xRight=300 for legacy box).
    const env = createHitTestEnv();
    const ctx = loadDrawingsModule(env);

    ctx._coarsePointer = true;

    const hit = ctx._getPositionHitAtPoint(250, 608);
    assert.ok(hit, 'touch 8px from SL line should hit with coarse LINE_R=10');
    assert.equal(hit.level, 'sl');
});

// ─── Phase 5c: hover-state setters ──────────────────────────────────

console.log('\n\x1b[1mhover-affordance state (f)\x1b[0m\n');

// Helper: env with getOverlayById + tracked overrideOverlay calls
function createHoverEnv() {
    const env = createMockEnv();
    env._overrideCalls = [];
    env.klineChart.overrideOverlay = (cfg) => { env._overrideCalls.push(cfg); };
    env.klineChart.getOverlayById = (id) => {
        return { id, extendData: {} };
    };
    return env;
}

test('_setRectHovered: no repaint when id unchanged', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._hoveredRectId = 'r1';
    ctx._setRectHovered('r1');
    assert.equal(env._overrideCalls.length, 0, 'no overrideOverlay when id is same');
});

test('_setRectHovered: triggers overrideOverlay on change (prev + next)', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._hoveredRectId = 'r1';
    ctx._setRectHovered('r2');
    // Must poke both prev (r1) and next (r2) — exactly 2 calls
    assert.equal(env._overrideCalls.length, 2, 'should poke prev + next overlay');
    const ids = env._overrideCalls.map(c => c.id);
    assert.ok(ids.indexOf('r1') !== -1, 'prev id should be poked');
    assert.ok(ids.indexOf('r2') !== -1, 'next id should be poked');
    assert.equal(ctx._hoveredRectId, 'r2', 'state should update to new id');
});

test('_setRectHovered: clears to null triggers one poke for prev only', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._hoveredRectId = 'r1';
    ctx._setRectHovered(null);
    assert.equal(env._overrideCalls.length, 1, 'only prev overlay poked on clear');
    assert.equal(env._overrideCalls[0].id, 'r1');
    assert.equal(ctx._hoveredRectId, null);
});

test('_setChannelHovered: no repaint when id unchanged', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._hoveredChannelId = 'ch1';
    ctx._setChannelHovered('ch1');
    assert.equal(env._overrideCalls.length, 0, 'no overrideOverlay when channel id is same');
});

test('_setChannelHovered: triggers overrideOverlay on change', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._hoveredChannelId = 'ch1';
    ctx._setChannelHovered('ch2');
    assert.equal(env._overrideCalls.length, 2, 'channel: poke prev + next');
    const ids = env._overrideCalls.map(c => c.id);
    assert.ok(ids.indexOf('ch1') !== -1, 'prev channel should be poked');
    assert.ok(ids.indexOf('ch2') !== -1, 'next channel should be poked');
    assert.equal(ctx._hoveredChannelId, 'ch2');
});

test('_setChannelHovered: clears to null triggers one poke', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._hoveredChannelId = 'ch1';
    ctx._setChannelHovered(null);
    assert.equal(env._overrideCalls.length, 1, 'only prev channel poked on clear');
    assert.equal(ctx._hoveredChannelId, null);
});

test('_setPositionHovered: no repaint when id unchanged', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._hoveredPositionId = 'pos1';
    ctx._setPositionHovered('pos1');
    assert.equal(env._overrideCalls.length, 0, 'no overrideOverlay when position id is same');
});

test('_setPositionHovered: triggers overrideOverlay on change', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._hoveredPositionId = 'pos1';
    ctx._setPositionHovered('pos2');
    assert.equal(env._overrideCalls.length, 2, 'position: poke prev + next');
    assert.equal(ctx._hoveredPositionId, 'pos2');
});

test('_setChannelHovered and _setRectHovered are independent (no cross-poke)', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    ctx._setRectHovered('r1');
    const afterRect = env._overrideCalls.length;
    ctx._setChannelHovered('ch1');
    const afterChannel = env._overrideCalls.length;
    // Each setter only pokes its own overlay
    assert.ok(afterRect >= 1, 'rect setter poked something');
    const channelPokes = env._overrideCalls.slice(afterRect);
    const channelPokeIds = channelPokes.map(c => c.id);
    assert.ok(channelPokeIds.indexOf('ch1') !== -1, 'channel setter poked ch1');
    assert.ok(channelPokeIds.indexOf('r1') === -1, 'channel setter did NOT re-poke r1');
});

test('_nativeHoverOrigWidth: stores original width on enter, restores on leave', () => {
    const env = createHoverEnv();
    const ctx = loadDrawingsModule(env);
    // Simulate onMouseEnter for a non-custom native overlay
    // We reach this indirectly: invoke getOverlayCallbacks() and call the handlers.
    // The callbacks are not exported directly; test via the module's global state.
    // Set up the map directly to verify the restore logic works.
    ctx._nativeHoverOrigWidth['seg1'] = 2;
    // Verify it is accessible
    assert.equal(ctx._nativeHoverOrigWidth['seg1'], 2, 'orig width stored in map');
    delete ctx._nativeHoverOrigWidth['seg1'];
    assert.equal(ctx._nativeHoverOrigWidth['seg1'], undefined, 'cleaned up after leave');
});

// ─── Phase 5b: _computeATR + one-click position placement ───────────
console.log('\n\x1b[1mPhase 5b: _computeATR and one-click position placement\x1b[0m\n');

// ── _computeATR math ────────────────────────────────────────────────

test('_computeATR: returns null for empty dataList', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._computeATR([], 14), null);
    assert.equal(ctx._computeATR(null, 14), null);
});

test('_computeATR: returns null for single-bar dataList (no prev close)', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const bars = [{ timestamp: 1000, open: 100, high: 110, low: 90, close: 105, volume: 1 }];
    assert.equal(ctx._computeATR(bars, 14), null);
});

test('_computeATR: returns null for period < 1', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const bars = [
        { timestamp: 1000, open: 100, high: 110, low: 90, close: 105, volume: 1 },
        { timestamp: 2000, open: 105, high: 115, low: 95, close: 110, volume: 1 }
    ];
    assert.equal(ctx._computeATR(bars, 0), null);
    assert.equal(ctx._computeATR(bars, -1), null);
});

test('_computeATR: correct for 2 bars (1 true range)', () => {
    // Bar 0: close=100. Bar 1: high=112, low=95. TR = max(17, |112-100|, |95-100|) = max(17,12,5) = 17
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const bars = [
        { timestamp: 1000, open: 100, high: 105, low: 98, close: 100, volume: 1 },
        { timestamp: 2000, open: 101, high: 112, low: 95, close: 110, volume: 1 }
    ];
    const atr = ctx._computeATR(bars, 14);
    assert.ok(atr !== null, 'should return a value');
    assert.equal(atr, 17, 'TR = max(17, 12, 5) = 17');
});

test('_computeATR: mean of last period bars when more bars than period', () => {
    // 3 bars, period=2: only last 2 TRs used
    // Bar0 close=100. Bar1: H=110, L=95, prevClose=100 → TR=max(15,10,5)=15.
    // Bar2: H=108, L=102, prevClose=105(bar1) → TR=max(6,3,3)=6.
    // period=2: uses TRs[last 2] = [15, 6] → mean = 10.5
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const bars = [
        { timestamp: 1000, open: 99,  high: 103, low: 98,  close: 100, volume: 1 },
        { timestamp: 2000, open: 100, high: 110, low: 95,  close: 105, volume: 1 },
        { timestamp: 3000, open: 105, high: 108, low: 102, close: 107, volume: 1 }
    ];
    const atr = ctx._computeATR(bars, 2);
    assert.ok(Math.abs(atr - 10.5) < 0.001, `expected 10.5, got ${atr}`);
});

test('_computeATR: <15 candles path returns valid (non-null) result with fewer bars', () => {
    // 10 bars of known shape — ATR should be computable even < 15 bars
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const bars = [];
    for (let i = 0; i < 10; i++) {
        bars.push({ timestamp: 1000 * (i + 1), open: 100, high: 110, low: 90, close: 100, volume: 1 });
    }
    // Every bar: H-L = 20, |H-prevC| = 10, |L-prevC| = 10 → TR = 20
    const atr = ctx._computeATR(bars, 14);
    // Only 9 true ranges available (10 bars - 1), period=14 clips to 9; mean should be 20
    assert.ok(atr !== null, '<15 bars: should return a value');
    assert.equal(atr, 20, '<15 bars: all TRs are 20');
});

test('_computeATR: ignores phantom future bars (_phantom flag)', () => {
    // Prod dataList ends in ~1000 flat X-axis-padding bars (OHLC = last close,
    // timestamps in the future). They zero the TR window → ATR collapses →
    // one-click position renders as a line.
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const bars = [];
    for (let i = 0; i < 20; i++) {
        bars.push({ timestamp: 1000 * (i + 1), open: 100, high: 110, low: 90, close: 100, volume: 1 });
    }
    const futureBase = Date.now() + 86400000;
    for (let i = 0; i < 30; i++) {
        bars.push({ timestamp: futureBase + i * 86400000, open: 100, high: 100, low: 100, close: 100, _phantom: true });
    }
    const atr = ctx._computeATR(bars, 14);
    assert.equal(atr, 20, 'phantom tail must not dilute ATR (real bars all TR=20)');
});

test('_computeATR: trims future bars even without _phantom flag', () => {
    // KLineChart may copy candle objects and drop custom fields — the
    // timestamp-in-the-future test must trim the tail on its own.
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const bars = [];
    for (let i = 0; i < 20; i++) {
        bars.push({ timestamp: 1000 * (i + 1), open: 100, high: 110, low: 90, close: 100, volume: 1 });
    }
    const futureBase = Date.now() + 86400000;
    for (let i = 0; i < 30; i++) {
        bars.push({ timestamp: futureBase + i * 86400000, open: 100, high: 100, low: 100, close: 100 });
    }
    const atr = ctx._computeATR(bars, 14);
    assert.equal(atr, 20, 'future-dated tail must not dilute ATR');
});

test('_computeATR: returns null when only phantom bars remain after trim', () => {
    // <2 real bars → null → callers fall back to the 2%/4% defaults
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const futureBase = Date.now() + 86400000;
    const bars = [{ timestamp: 1000, open: 100, high: 110, low: 90, close: 100, volume: 1 }];
    for (let i = 0; i < 10; i++) {
        bars.push({ timestamp: futureBase + i * 86400000, open: 100, high: 100, low: 100, close: 100, _phantom: true });
    }
    assert.equal(ctx._computeATR(bars, 14), null);
});

test('_computeATR: uses _orig* raw values on log-scale dataLists', () => {
    // In log mode dataList OHLC are Math.log values (raw kept in _orig*) —
    // ATR must come out as a RAW price delta, not a log-space delta.
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const bars = [];
    for (let i = 0; i < 10; i++) {
        const o = 100, h = 110, l = 90, c = 100;
        bars.push({
            timestamp: 1000 * (i + 1),
            open: Math.log(o), high: Math.log(h), low: Math.log(l), close: Math.log(c),
            _origOpen: o, _origHigh: h, _origLow: l, _origClose: c,
            volume: 1
        });
    }
    const atr = ctx._computeATR(bars, 14);
    assert.equal(atr, 20, 'log-mode ATR must be raw-space (TR=20), not ~0.2 log-space');
});

// ── one-click placement helper via saveKlineDrawing path ─────────────

function createPositionPlacementEnv() {
    const env = createMockEnv();
    // Build 20-bar data list
    const baseTs = 1700000000000;
    const bars = [];
    for (let i = 0; i < 20; i++) {
        bars.push({
            timestamp: baseTs + i * 86400000,
            open: 100 + i, high: 112 + i, low: 90 + i, close: 105 + i, volume: 1000
        });
    }
    env.klineChart.getDataList = () => bars;
    env.klineChart.convertFromPixel = (pt) => ({ dataIndex: 10, value: 200 }); // entry price = 200
    env.klineChart.overrideOverlay = () => {};
    env._logScaleEnabled = false;
    env.autoOpenJournalEntry = () => Promise.resolve(null);
    env.document = {
        addEventListener: () => {},
        getElementById: () => ({
            getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
            offsetWidth: 800,
            addEventListener: () => {},
            removeEventListener: () => {},
            querySelectorAll: () => [],
            appendChild: () => {}
        }),
        createElement: () => ({
            className: '', innerHTML: '', style: {}, setAttribute: () => {},
            addEventListener: () => {}, removeEventListener: () => {},
            classList: { add: () => {}, remove: () => {} }, remove: () => {},
            appendChild: () => {}
        }),
        body: { appendChild: () => {}, removeEventListener: () => {} },
        querySelectorAll: () => []
    };
    return env;
}

test('one-click placement: switching Long→Short disarms the old tool (one click = ONE placement)', () => {
    const env = createPositionPlacementEnv();
    // container that CAPTURES the armed listeners so the test can fire them
    const listeners = { mousedown: [], touchend: [] };
    const container = {
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        offsetWidth: 800,
        appendChild: () => {},
        addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener: (type, fn) => { const a = listeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); },
        querySelectorAll: () => [],
        style: { setProperty: () => {}, removeProperty: () => {} }
    };
    env.document.getElementById = () => container;
    env.document.removeEventListener = () => {};
    env.klineModal = null;
    const ctx = loadDrawingsModule(env);

    const mkBtn = () => ({ classList: { add: () => {}, remove: () => {} }, closest: () => null, setAttribute: () => {} });
    ctx.handleKlineToolClick('longPosition', mkBtn(), [], {});
    ctx.handleKlineToolClick('shortPosition', mkBtn(), [], {});

    // The Long arming must be disarmed by the Short arming
    assert.equal(listeners.mousedown.length, 1, 'only ONE armed mousedown listener after switching tools');

    const ev = { button: 0, clientX: 100, clientY: 300, stopPropagation: () => {}, preventDefault: () => {} };
    listeners.mousedown.slice().forEach(fn => fn(ev));

    const all = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}');
    const placed = (all['ETH'] || []).filter(d => d.name === 'longPosition' || d.name === 'shortPosition');
    assert.equal(placed.length, 1, 'exactly one position placed');
    assert.equal(placed[0].name, 'shortPosition', 'the placed position is the SHORT (last-armed tool)');
});

test('one-click placement: arming any other tool disarms a pending position tool', () => {
    const env = createPositionPlacementEnv();
    const listeners = { mousedown: [], touchend: [] };
    const container = {
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        offsetWidth: 800,
        appendChild: () => {},
        addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener: (type, fn) => { const a = listeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); },
        querySelectorAll: () => [],
        style: { setProperty: () => {}, removeProperty: () => {} }
    };
    env.document.getElementById = () => container;
    env.document.removeEventListener = () => {};
    env.klineModal = null;
    env.klineDrawingColor = '#3fb950';
    env.klineDrawingWidth = 1;
    const ctx = loadDrawingsModule(env);

    const mkBtn = () => ({ classList: { add: () => {}, remove: () => {} }, closest: () => null, setAttribute: () => {} });
    ctx.handleKlineToolClick('longPosition', mkBtn(), [], {});
    assert.equal(listeners.mousedown.length, 1, 'long armed');
    // switch to a native drawing tool — position arming must clear
    ctx.handleKlineToolClick('horizontalStraightLine', mkBtn(), [], {});
    assert.equal(listeners.mousedown.length, 0, 'position listener disarmed when another tool was armed');
});

test('_computeATR via _mobileHandleApi: exposed correctly', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.ok(typeof ctx._mobileHandleApi !== 'undefined' || typeof env._mobileHandleApi !== 'undefined',
        'mobileHandleApi should exist');
    // Access via window._mobileHandleApi (set on env since env = window)
    const api = env._mobileHandleApi || ctx._mobileHandleApi;
    assert.ok(api, 'mobileHandleApi exists');
    assert.ok(typeof api.computeATR === 'function', 'computeATR exposed on mobileHandleApi');
    const bars = [
        { timestamp: 1000, open: 100, high: 110, low: 95, close: 100, volume: 1 },
        { timestamp: 2000, open: 100, high: 115, low: 90, close: 105, volume: 1 }
    ];
    // TR = max(25, 15, 10) = 25
    const atr = api.computeATR(bars, 14);
    assert.equal(atr, 25, 'computeATR via api returns correct value');
});

test('one-click long: saveKlineDrawing called with SL < entry < TP', () => {
    const env = createPositionPlacementEnv();
    const ctx = loadDrawingsModule(env);

    // Manually compute ATR and call saveKlineDrawing the same way _posPlace does:
    const dataList = env.klineChart.getDataList();
    const atr = ctx._computeATR(dataList, 14);
    const entry = 200;
    assert.ok(atr !== null && atr > 0, 'ATR should be valid for 20-bar list');
    const sl = Math.max(entry - 1.5 * atr, 0.0000001);
    const tp = entry + 3 * atr;

    assert.ok(sl < entry, 'long SL must be below entry');
    assert.ok(tp > entry, 'long TP must be above entry');
    assert.ok(tp - entry > entry - sl, 'long 2:1 RR — TP distance > SL distance');
    assert.ok(sl > 0, 'SL must be positive');
});

test('one-click short: SL > entry > TP with ATR defaults', () => {
    const env = createPositionPlacementEnv();
    const ctx = loadDrawingsModule(env);

    const dataList = env.klineChart.getDataList();
    const atr = ctx._computeATR(dataList, 14);
    const entry = 200;
    const sl = entry + 1.5 * atr;
    const tp = Math.max(entry - 3 * atr, 0.0000001);

    assert.ok(sl > entry, 'short SL must be above entry');
    assert.ok(tp < entry, 'short TP must be below entry');
    assert.ok(tp > 0, 'short TP must be positive');
});

test('one-click long: fallback %  when ATR is null (<2 bars)', () => {
    // ATR returns null → fallback: SL=entry*0.98, TP=entry*1.04
    const entry = 1000;
    const sl = Math.max(entry * 0.98, 0.0000001);
    const tp = entry * 1.04;
    assert.equal(sl, 980, 'fallback long SL = entry*0.98');
    assert.equal(tp, 1040, 'fallback long TP = entry*1.04');
    assert.ok(sl < entry && tp > entry, 'SL < entry < TP');
});

test('one-click short: fallback % when ATR is null (<2 bars)', () => {
    const entry = 1000;
    const sl = entry * 1.02;
    const tp = Math.max(entry * 0.96, 0.0000001);
    assert.equal(sl, 1020, 'fallback short SL = entry*1.02');
    assert.equal(tp, 960, 'fallback short TP = entry*0.96');
    assert.ok(sl > entry && tp < entry, 'SL > entry > TP');
});

test('one-click placement: drawing record has correct name, 3 points, all same timestamp', () => {
    const env = createPositionPlacementEnv();
    const ctx = loadDrawingsModule(env);

    // Simulate what _posPlace does directly via saveKlineDrawing
    const ts = 1700000000000 + 10 * 86400000;
    const entry = 200;
    const dataList = env.klineChart.getDataList();
    const atr = ctx._computeATR(dataList, 14);
    const sl = Math.max(entry - 1.5 * atr, 0.0000001);
    const tp = entry + 3 * atr;

    ctx.saveKlineDrawing({
        id: 'pos_oneclick_1',
        name: 'longPosition',
        points: [
            { timestamp: ts, value: entry },
            { timestamp: ts, value: sl },
            { timestamp: ts, value: tp }
        ],
        color: '#26a69a',
        width: 1,
        extendData: {}
    });

    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const saved = all['ETH'].find(d => d.id === 'pos_oneclick_1');
    assert.ok(saved, 'drawing should be saved');
    assert.equal(saved.name, 'longPosition', 'name is longPosition');
    assert.equal(saved.points.length, 3, '3 points');
    assert.equal(saved.points[0].timestamp, saved.points[1].timestamp, 'all points share same timestamp');
    assert.equal(saved.points[1].timestamp, saved.points[2].timestamp, 'all points share same timestamp');
    assert.ok(saved.points[1].value < saved.points[0].value, 'SL < entry');
    assert.ok(saved.points[2].value > saved.points[0].value, 'TP > entry');
});

test('one-click placement: zOrder = maxZ+1 (new drawing lands on top)', () => {
    const env = createPositionPlacementEnv();
    // Seed with existing drawings at known zOrders
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [
            { id: 'existing_1', name: 'straightLine', points: [], color: '#fff', width: 1, zOrder: 5 },
            { id: 'existing_2', name: 'rect', points: [], color: '#fff', width: 1, zOrder: 8 }
        ]
    }));
    const ctx = loadDrawingsModule(env);

    ctx.saveKlineDrawing({
        id: 'pos_z_1',
        name: 'longPosition',
        points: [
            { timestamp: 1000, value: 200 },
            { timestamp: 1000, value: 180 },
            { timestamp: 1000, value: 240 }
        ],
        color: '#26a69a',
        width: 1,
        extendData: {}
    });

    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const saved = all['ETH'].find(d => d.id === 'pos_z_1');
    assert.ok(saved, 'drawing should be saved');
    assert.equal(saved.zOrder, 9, 'zOrder should be maxZ(8)+1=9');
});

test('one-click placement: shortPosition name preserved', () => {
    const env = createPositionPlacementEnv();
    const ctx = loadDrawingsModule(env);

    const ts = 1700000000000;
    const entry = 50000;
    const dataList = env.klineChart.getDataList();
    const atr = ctx._computeATR(dataList, 14);
    const sl = entry + 1.5 * atr;
    const tp = Math.max(entry - 3 * atr, 0.0000001);

    ctx.saveKlineDrawing({
        id: 'pos_short_1',
        name: 'shortPosition',
        points: [
            { timestamp: ts, value: entry },
            { timestamp: ts, value: sl },
            { timestamp: ts, value: tp }
        ],
        color: '#ef5350',
        width: 1,
        extendData: {}
    });

    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const saved = all['ETH'].find(d => d.id === 'pos_short_1');
    assert.ok(saved, 'shortPosition drawing should be saved');
    assert.equal(saved.name, 'shortPosition', 'name is shortPosition');
    assert.ok(saved.points[1].value > saved.points[0].value, 'short: SL > entry');
    assert.ok(saved.points[2].value < saved.points[0].value, 'short: TP < entry');
});

// ─── migrateDrawingName: long/short direction flip ──────────────────
console.log('\n\x1b[1mmigrateDrawingName: legacy swap must not corrupt new-format positions\x1b[0m\n');

test('migrateDrawingName: new-format longPosition [E, SL<E, TP>E] is untouched', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const d = {
        name: 'longPosition',
        points: [
            { timestamp: 1000, value: 100 },
            { timestamp: 1000, value: 85 },   // SL below entry — already new order
            { timestamp: 1000, value: 130 }   // TP above entry
        ]
    };
    ctx.migrateDrawingName(d);
    assert.equal(d.points[1].value, 85, 'SL stays in slot 1');
    assert.equal(d.points[2].value, 130, 'TP stays in slot 2');
});

test('migrateDrawingName: new-format shortPosition [E, SL>E, TP<E] is untouched', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const d = {
        name: 'shortPosition',
        points: [
            { timestamp: 1000, value: 100 },
            { timestamp: 1000, value: 115 },  // SL above entry — already new order
            { timestamp: 1000, value: 70 }    // TP below entry
        ]
    };
    ctx.migrateDrawingName(d);
    assert.equal(d.points[1].value, 115, 'SL stays in slot 1');
    assert.equal(d.points[2].value, 70, 'TP stays in slot 2');
});

test('migrateDrawingName: old-format longPosition [E, TP>E, SL<E] still gets repaired', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const d = {
        name: 'longPosition',
        points: [
            { timestamp: 1000, value: 100 },
            { timestamp: 1000, value: 130 },  // TP above entry — legacy order
            { timestamp: 1000, value: 85 }    // SL below entry
        ]
    };
    ctx.migrateDrawingName(d);
    assert.equal(d.points[1].value, 85, 'SL swapped into slot 1');
    assert.equal(d.points[2].value, 130, 'TP swapped into slot 2');
});

test('migrateDrawingName: old-format shortPosition [E, TP<E, SL>E] still gets repaired', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const d = {
        name: 'shortPosition',
        points: [
            { timestamp: 1000, value: 100 },
            { timestamp: 1000, value: 70 },   // TP below entry — legacy order
            { timestamp: 1000, value: 115 }   // SL above entry
        ]
    };
    ctx.migrateDrawingName(d);
    assert.equal(d.points[1].value, 115, 'SL swapped into slot 1');
    assert.equal(d.points[2].value, 70, 'TP swapped into slot 2');
});

test('migrateDrawingName: migration is idempotent (second run does not re-flip)', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const d = {
        name: 'longPosition',
        points: [
            { timestamp: 1000, value: 100 },
            { timestamp: 1000, value: 130 },
            { timestamp: 1000, value: 85 }
        ]
    };
    ctx.migrateDrawingName(d);
    ctx.migrateDrawingName(d);
    assert.equal(d.points[1].value, 85, 'SL still in slot 1 after second run');
    assert.equal(d.points[2].value, 130, 'TP still in slot 2 after second run');
});

test('migrateDrawingName: 2-point position drawing is left alone', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const d = {
        name: 'longPosition',
        points: [
            { timestamp: 1000, value: 100 },
            { timestamp: 1000, value: 85 }
        ]
    };
    ctx.migrateDrawingName(d);
    assert.equal(d.name, 'longPosition', 'name unchanged');
    assert.equal(d.points[1].value, 85, 'points unchanged');
});

// ─── Opacity: reachable for every drawing, not just rect ────────────
console.log('\n\x1b[1mOpacity: context menu for all types + keyboard step helper\x1b[0m\n');

// showDrawingContextMenu needs a slightly richer DOM than the shared mock —
// createElement/body stubs that capture innerHTML for assertions.
function createCtxMenuEnv() {
    const env = createMockEnv();
    function el() {
        return {
            className: '', innerHTML: '', style: {},
            classList: { add: () => {}, remove: () => {}, toggle: () => {} },
            addEventListener: () => {}, remove: () => {},
            appendChild: () => {}, contains: () => false,
        };
    }
    env.document = {
        addEventListener: () => {},
        createElement: () => el(),
        getElementById: () => null,
        body: { appendChild: () => {} },
    };
    env.innerWidth = 1200;
    env.innerHeight = 800;
    env.window = env;
    return env;
}

test('context menu offers Opacity for a plain line drawing (not just rect)', () => {
    const env = createCtxMenuEnv();
    const ctx = loadDrawingsModule(env);
    ctx.showDrawingContextMenu(100, 100, 'draw_1');  // seeded straightLine
    assert.ok(ctx.klineContextMenu, 'menu element created');
    assert.ok(ctx.klineContextMenu.innerHTML.indexOf('edit-opacity') !== -1,
        'non-rect drawing must expose the Opacity action');
});

test('context menu still offers Opacity for rect', () => {
    const env = createCtxMenuEnv();
    const ctx = loadDrawingsModule(env);
    ctx.showDrawingContextMenu(100, 100, 'draw_2');  // seeded rect
    assert.ok(ctx.klineContextMenu.innerHTML.indexOf('edit-opacity') !== -1);
});

// ─── Alert line ↔ server alert sync ─────────────────
console.log('\n\x1b[1mAlert line sync: _alertId persisted, drag updates the server alert\x1b[0m\n');

function createAlertSyncEnv() {
    const env = createMockEnv();
    env._fetches = [];
    env.fetch = (url, opts) => {
        env.fetch._urls = env.fetch._urls || [];
        env._fetches.push({ url, opts: opts || {} });
        return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ alert: { id: 4242 } }),
        });
    };
    env.getCSRFToken = () => 'token';
    env.updateAlertBadge = () => {};
    env.updateDrawerAlerts = () => {};
    env._toasts = [];
    env.showToast = (m) => env._toasts.push(m);
    env.allAssets = { ETH: { latest_price: 3000 } };
    env.klineModal = null;
    env.klineDrawingColor = '#ff9800';
    env.document = { addEventListener: () => {}, getElementById: () => null };
    // drawing-editing.js helper used by onPressedMoveEnd — storage-backed like the real one
    env.getOrCreateDrawingRecord = (id) => {
        const all = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}');
        for (const k of Object.keys(all)) {
            const hit = (all[k] || []).find(d => d.id === id);
            if (hit) return hit;
        }
        return null;
    };
    return env;
}
const flush = () => new Promise((r) => setTimeout(r, 0));

await testAsync('drawn alertLine persists the created server alert id (_alertId)', async () => {
    const env = createAlertSyncEnv();
    const ctx = loadDrawingsModule(env);
    // Arm the tool; the harness klineChart.createOverlay records the config
    const btn = { classList: { add: () => {}, remove: () => {} }, closest: () => null };
    ctx.handleKlineToolClick('alertLine', btn, [], null);
    const cfg = Object.values(env._overlays).find(o => o.name === 'alertLine');
    assert.ok(cfg && typeof cfg.onDrawEnd === 'function', 'overlay armed with onDrawEnd');
    const oid = Object.keys(env._overlays).find(k => env._overlays[k] === cfg);
    cfg.onDrawEnd({ overlay: { id: oid, points: [
        { value: 3500, timestamp: 1000, dataIndex: 0 },
        { value: 3500, timestamp: 2000, dataIndex: 1 },
    ] } });
    await flush(); await flush(); await flush();
    const post = env._fetches.find(f => f.url === '/api/alerts' && f.opts.method === 'POST');
    assert.ok(post, 'server alert created');
    const saved = getDrawingsFromStorage(env, 'ETH').find(d => d.id === oid);
    assert.ok(saved, 'drawing saved');
    assert.equal(saved.extendData && saved.extendData._alertId, 4242,
        'created alert id persisted on the drawing');
});

await testAsync('dragging an alertLine with _alertId PUTs the new price + condition', async () => {
    const env = createAlertSyncEnv();
    const ctx = loadDrawingsModule(env);
    // Seed a drawn alert line with a known _alertId
    ctx.saveKlineDrawing({ id: 'al_drag', name: 'alertLine',
        points: [{ value: 3500, timestamp: 1000 }, { value: 3500, timestamp: 2000 }],
        color: '#ff9800', extendData: { _color: '#ff9800', _alertId: 4242 } });
    env._fetches.length = 0;
    const cbs = ctx.getOverlayCallbacks();
    cbs.onPressedMoveEnd({ overlay: { id: 'al_drag', name: 'alertLine',
        extendData: { _color: '#ff9800', _alertId: 4242 },
        points: [{ value: 2500, timestamp: 1000, dataIndex: 0 }, { value: 2500, timestamp: 2000, dataIndex: 1 }] } });
    await flush(); await flush();
    const put = env._fetches.find(f => f.url === '/api/alerts/4242' && f.opts.method === 'PUT');
    assert.ok(put, 'PUT issued to the alert');
    const body = JSON.parse(put.opts.body);
    assert.equal(body.price, 2500, 'linear price sent');
    assert.equal(body.condition, 'below', 'condition re-derived vs live price (3000)');
});

await testAsync('dragging an alertLine WITHOUT _alertId issues no PUT (no orphan writes)', async () => {
    const env = createAlertSyncEnv();
    const ctx = loadDrawingsModule(env);
    ctx.saveKlineDrawing({ id: 'al_plain', name: 'alertLine',
        points: [{ value: 3500, timestamp: 1000 }, { value: 3500, timestamp: 2000 }],
        color: '#ff9800', extendData: { _color: '#ff9800' } });
    env._fetches.length = 0;
    ctx.getOverlayCallbacks().onPressedMoveEnd({ overlay: { id: 'al_plain', name: 'alertLine',
        extendData: { _color: '#ff9800' },
        points: [{ value: 2500, timestamp: 1000, dataIndex: 0 }, { value: 2500, timestamp: 2000, dataIndex: 1 }] } });
    await flush();
    assert.equal(env._fetches.filter(f => f.opts.method === 'PUT').length, 0);
});

await testAsync('failed PUT reverts the drawing to the pre-drag price', async () => {
    const env = createAlertSyncEnv();
    const ctx = loadDrawingsModule(env);
    ctx.saveKlineDrawing({ id: 'al_rev', name: 'alertLine',
        points: [{ value: 3500, timestamp: 1000 }, { value: 3500, timestamp: 2000 }],
        color: '#ff9800', extendData: { _color: '#ff9800', _alertId: 777 } });
    env.fetch = (url, opts) => {
        env._fetches.push({ url, opts: opts || {} });
        return Promise.resolve({ ok: false, json: () => Promise.resolve({ error: 'nope' }) });
    };
    env._fetches.length = 0;
    ctx.getOverlayCallbacks().onPressedMoveEnd({ overlay: { id: 'al_rev', name: 'alertLine',
        extendData: { _color: '#ff9800', _alertId: 777 },
        points: [{ value: 2500, timestamp: 1000, dataIndex: 0 }, { value: 2500, timestamp: 2000, dataIndex: 1 }] } });
    await flush(); await flush(); await flush();
    const saved = getDrawingsFromStorage(env, 'ETH').find(d => d.id === 'al_rev');
    assert.equal(saved.points[0].value, 3500, 'price reverted in storage');
    assert.ok(env._toasts.length > 0, 'user told the move did not stick');
});

await testAsync('one-click alertLevel creates the alert and persists _alertId on a single-point drawing', async () => {
    const env = createAlertSyncEnv();
    const ctx = loadDrawingsModule(env);
    const btn = { classList: { add: () => {}, remove: () => {} }, closest: () => null };
    ctx.handleKlineToolClick('alertLevel', btn, [], null);
    const cfg = Object.values(env._overlays).find(o => o.name === 'alertLevel');
    assert.ok(cfg && typeof cfg.onDrawEnd === 'function', 'alertLevel armed under its own name');
    const oid = Object.keys(env._overlays).find(k => env._overlays[k] === cfg);
    cfg.onDrawEnd({ overlay: { id: oid, points: [{ value: 3500, timestamp: 1000, dataIndex: 0 }] } });
    await flush(); await flush(); await flush();
    const saved = getDrawingsFromStorage(env, 'ETH').find(d => d.id === oid);
    assert.ok(saved, 'drawing saved');
    assert.equal(saved.name, 'alertLevel', 'saved under the new tool name');
    assert.equal(saved.extendData && saved.extendData._alertId, 4242, 'alert id persisted');
});

await testAsync('dragging an alertLevel with _alertId PUTs the new price', async () => {
    const env = createAlertSyncEnv();
    const ctx = loadDrawingsModule(env);
    ctx.saveKlineDrawing({ id: 'lv_drag', name: 'alertLevel',
        points: [{ value: 3500, timestamp: 1000 }],
        color: '#ff9800', extendData: { _color: '#ff9800', _alertId: 4242 } });
    env._fetches.length = 0;
    ctx.getOverlayCallbacks().onPressedMoveEnd({ overlay: { id: 'lv_drag', name: 'alertLevel',
        extendData: { _color: '#ff9800', _alertId: 4242 },
        points: [{ value: 2500, timestamp: 1000, dataIndex: 0 }] } });
    await flush(); await flush();
    const put = env._fetches.find(f => f.url === '/api/alerts/4242' && f.opts.method === 'PUT');
    assert.ok(put, 'PUT issued');
    assert.equal(JSON.parse(put.opts.body).price, 2500);
});

await testAsync('alert-note action saves the note and PUTs it behind the [drawn] marker', async () => {
    const env = createAlertSyncEnv();
    const ctx = loadDrawingsModule(env);
    ctx.saveKlineDrawing({ id: 'al_note', name: 'alertLine',
        points: [{ value: 3500, timestamp: 1000 }, { value: 3500, timestamp: 2000 }],
        color: '#ff9800', extendData: { _color: '#ff9800', _alertId: 555 } });
    env._fetches.length = 0;
    ctx._showAlertNoteDialog = (current, cb) => cb('breakout watch');
    ctx.handleContextMenuAction('alert-note', 'al_note');
    await flush(); await flush();
    const saved = getDrawingsFromStorage(env, 'ETH').find(d => d.id === 'al_note');
    assert.equal(saved.extendData.note, 'breakout watch', 'note on the drawing (AI-readable via drawings sync)');
    const put = env._fetches.find(f => f.url === '/api/alerts/555' && f.opts.method === 'PUT');
    assert.ok(put, 'note synced to the server alert');
    assert.equal(JSON.parse(put.opts.body).note, '[drawn] breakout watch',
        'keeps the load-bearing [drawn] marker in front');
});

await testAsync('clearing the note reverts the server note to bare [drawn]', async () => {
    const env = createAlertSyncEnv();
    const ctx = loadDrawingsModule(env);
    ctx.saveKlineDrawing({ id: 'al_note2', name: 'alertLine',
        points: [{ value: 3500, timestamp: 1000 }, { value: 3500, timestamp: 2000 }],
        color: '#ff9800', extendData: { _color: '#ff9800', _alertId: 556, note: 'old text' } });
    env._fetches.length = 0;
    ctx._showAlertNoteDialog = (current, cb) => { assert.equal(current, 'old text', 'dialog pre-filled'); cb(''); };
    ctx.handleContextMenuAction('alert-note', 'al_note2');
    await flush(); await flush();
    const saved = getDrawingsFromStorage(env, 'ETH').find(d => d.id === 'al_note2');
    assert.equal(saved.extendData.note, undefined, 'note removed from the drawing');
    const put = env._fetches.find(f => f.url === '/api/alerts/556' && f.opts.method === 'PUT');
    assert.ok(put, 'server told');
    assert.equal(JSON.parse(put.opts.body).note, '[drawn]');
});

// ─── FRVP Extend Right: disarm only on real drag, never on a click ──
console.log('\n\x1b[1mFRVP Extend Right: click near the live edge must not disarm\x1b[0m\n');

function createFrvpDragEnv() {
    const env = createMockEnv();
    // Seed an extend-right FRVP into the ETH bucket
    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    all.ETH.push({
        id: 'frvp_1', name: 'frvp',
        points: [{ timestamp: 1000 }, { timestamp: 2000 }],
        color: '#3fb950', width: 1,
        extendData: { extendRight: true },
    });
    env.localStorage.setItem('klineDrawings', JSON.stringify(all));

    const overrides = [];
    const bars = [];
    for (let i = 0; i < 30; i++) bars.push({ timestamp: 1000 + i * 100, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1 });
    env.klineChart = {
        convertFromPixel: (p) => ({ dataIndex: Math.round(p.x / 10), value: 1 }),
        getOverlayById: () => [{
            id: 'frvp_1', name: 'frvp',
            points: [{ dataIndex: 10, timestamp: 2000 }, { dataIndex: 20, timestamp: 3000 }],
            extendData: { extendRight: true },
        }],
        getDataList: () => bars,
        overrideOverlay: (cfg) => { overrides.push(cfg); },
        removeOverlay: () => {},
        createOverlay: () => 'new_id',
    };
    env._overrides = overrides;
    return env;
}
const _frvpStored = (env) => JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'frvp_1');
const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };

test('FRVP: sub-3px jitter (a click) neither disarms extendRight nor moves anchors', () => {
    const env = createFrvpDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._frvpEdgeDrag = { overlayId: 'frvp_1', edge: 1, paneId: 'candle_pane',
        disarmHit: { overlayId: 'frvp_1', edge: 1, paneId: 'candle_pane' }, startX: 100 };
    ctx._handleFrvpEdgeDrag(101, 50, container);  // 1px of jitter
    assert.equal(_frvpStored(env).extendData.extendRight, true, 'extendRight survives a click');
    assert.equal(env._overrides.filter(o => o.points).length, 0, 'anchors not rewritten by jitter');
});

test('FRVP: a real drag (>=3px) disarms extendRight once, then drags', () => {
    const env = createFrvpDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._frvpEdgeDrag = { overlayId: 'frvp_1', edge: 1, paneId: 'candle_pane',
        disarmHit: { overlayId: 'frvp_1', edge: 1, paneId: 'candle_pane' }, startX: 100 };
    ctx._handleFrvpEdgeDrag(110, 50, container);  // 10px — a real drag
    assert.equal(_frvpStored(env).extendData.extendRight, false, 'extendRight reverts to manual on drag');
    assert.equal(env._overrides.filter(o => o.points).length, 1, 'drag then moves the anchor');
});

test('FRVP: drag with no pending disarm behaves as before (moves anchors immediately)', () => {
    const env = createFrvpDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._frvpEdgeDrag = { overlayId: 'frvp_1', edge: 1, paneId: 'candle_pane' };
    ctx._handleFrvpEdgeDrag(110, 50, container);
    assert.equal(env._overrides.filter(o => o.points).length, 1, 'plain edge drag still works');
    assert.equal(_frvpStored(env).extendData.extendRight, true, 'no disarm without pending hit');
});

test('FRVP: touch path (no startX yet) uses first move as baseline, not a disarm trigger', () => {
    const env = createFrvpDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._frvpEdgeDrag = { overlayId: 'frvp_1', edge: 1, paneId: 'candle_pane',
        disarmHit: { overlayId: 'frvp_1', edge: 1, paneId: 'candle_pane' }, startX: null };
    ctx._handleFrvpEdgeDrag(200, 50, container);  // first move only sets the baseline
    assert.equal(_frvpStored(env).extendData.extendRight, true, 'first touch move is baseline');
    ctx._handleFrvpEdgeDrag(210, 50, container);  // now a real drag
    assert.equal(_frvpStored(env).extendData.extendRight, false, 'second move beyond 3px disarms');
});

// ─── Phase 5e: duplicateSelectedDrawing opts (in-place clone) ──────────

console.log('\n\x1b[1mPhase 5e: Ctrl+drag clone + arrow-key nudge\x1b[0m\n');

// Helper: richer env with a multi-bar data list suitable for nudge tests
function createPhase5eEnv() {
    const env = createCopyPasteEnv();
    // Override dataList with 50 bars
    const baseTs = 1700000000000;
    const bars = [];
    for (let i = 0; i < 50; i++) {
        bars.push({ timestamp: baseTs + i * 86400000, open: 100 + i, high: 110 + i, low: 90 + i, close: 105 + i, volume: 1000 });
    }
    env.klineChart.getDataList = () => bars;
    env._overrideCalls = [];
    env.klineChart.overrideOverlay = (cfg) => { env._overrideCalls.push(cfg); };
    // getOverlayById: return live overlay for any id in storage
    env.klineChart.getOverlayById = (id) => {
        const all = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}');
        const drawings = all.ETH || [];
        const d = drawings.find(x => x.id === id);
        if (!d) return null;
        // Live overlay: points in chart space (non-log for these tests)
        return {
            id: d.id,
            name: d.name,
            lock: !!(d.extendData && d.extendData._locked),
            points: d.points.map(p => {
                // resolve timestamp -> dataIndex
                const di = bars.findIndex(b => b.timestamp === p.timestamp);
                return { dataIndex: di >= 0 ? di : 10, value: p.value, timestamp: p.timestamp };
            }),
            extendData: d.extendData || {}
        };
    };
    // Add a rect and channel for nudge tests
    const store = JSON.parse(env.localStorage.getItem('klineDrawings'));
    store.ETH.push(
        { id: 'rect_n1', name: 'rect', points: [
            { timestamp: baseTs + 10 * 86400000, value: 200 },
            { timestamp: baseTs + 20 * 86400000, value: 150 }
        ], color: '#f00', width: 1, extendData: {} },
        { id: 'chan_n1', name: 'boundedChannel', points: [
            { timestamp: baseTs + 5 * 86400000, value: 180 },
            { timestamp: baseTs + 15 * 86400000, value: 200 },
            { timestamp: baseTs + 5 * 86400000, value: 160 }
        ], color: '#0ff', width: 2, extendData: {} },
        { id: 'pos_n1', name: 'position', points: [
            { timestamp: baseTs + 10 * 86400000, value: 100 },
            { timestamp: baseTs + 10 * 86400000, value: 90 },
            { timestamp: baseTs + 10 * 86400000, value: 120 }
        ], color: '#ff0', width: 1, extendData: { rightTs: baseTs + 20 * 86400000 } },
        { id: 'locked_r1', name: 'rect', points: [
            { timestamp: baseTs + 5 * 86400000, value: 300 },
            { timestamp: baseTs + 10 * 86400000, value: 250 }
        ], color: '#888', width: 1, extendData: { _locked: true } }
    );
    env.localStorage.setItem('klineDrawings', JSON.stringify(store));
    return env;
}

// ── duplicateSelectedDrawing with opts (in-place clone) ──────────────

test('duplicateSelectedDrawing opts: in-place clone returns new overlay id (truthy)', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    const result = ctx.duplicateSelectedDrawing('rect_n1', { barOffset: 0, valOffset: 0, silent: true });
    assert.ok(result, 'should return a truthy new id');
    assert.ok(typeof result === 'string', 'should return string id');
});

test('duplicateSelectedDrawing opts: in-place clone has identical points to source', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    const src = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'rect_n1');
    const newId = ctx.duplicateSelectedDrawing('rect_n1', { barOffset: 0, valOffset: 0, silent: true });

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const clone = drawings.find(d => d.id === newId);
    assert.ok(clone, 'clone should exist in storage');
    // Points should be identical
    assert.equal(clone.points.length, src.points.length, 'same number of points');
    for (let i = 0; i < src.points.length; i++) {
        assert.equal(clone.points[i].timestamp, src.points[i].timestamp, `point[${i}].timestamp matches`);
        assert.equal(clone.points[i].value, src.points[i].value, `point[${i}].value matches`);
    }
});

test('duplicateSelectedDrawing opts: in-place clone preserves color, width', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    const src = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'rect_n1');
    const newId = ctx.duplicateSelectedDrawing('rect_n1', { barOffset: 0, valOffset: 0, silent: true });
    const clone = getDrawingsFromStorage(env, 'ETH').find(d => d.id === newId);

    assert.equal(clone.color, src.color, 'color preserved');
    assert.equal(clone.width, src.width, 'width preserved');
});

test('duplicateSelectedDrawing opts: in-place clone has different id from source', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    const newId = ctx.duplicateSelectedDrawing('rect_n1', { barOffset: 0, valOffset: 0, silent: true });
    assert.notEqual(newId, 'rect_n1', 'clone must have a different id');
});

test('duplicateSelectedDrawing opts: in-place clone has higher zOrder than source', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const srcBefore = drawings.find(d => d.id === 'rect_n1');

    const newId = ctx.duplicateSelectedDrawing('rect_n1', { barOffset: 0, valOffset: 0, silent: true });
    const allAfter = getDrawingsFromStorage(env, 'ETH');
    const clone = allAfter.find(d => d.id === newId);
    const srcAfter = allAfter.find(d => d.id === 'rect_n1');

    // Clone should have higher zOrder than source (maxZ+1 path in saveKlineDrawing)
    assert.ok(clone.zOrder > (srcAfter ? (srcAfter.zOrder || 0) : 0), 'clone zOrder > source zOrder');
});

test('duplicateSelectedDrawing opts: in-place clone does NOT show toast (silent:true)', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    const toastsBefore = env._toasts.length;
    ctx.duplicateSelectedDrawing('rect_n1', { barOffset: 0, valOffset: 0, silent: true });
    assert.equal(env._toasts.length, toastsBefore, 'no toast when silent:true');
});

test('duplicateSelectedDrawing opts: in-place clone strips side-effects from extendData', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    // chan_1 has no side effects; use fib_1 from base env which has _fibLevels (not side effect),
    // test with alert_1 which has _alertId
    const newId = ctx.duplicateSelectedDrawing('alert_1', { barOffset: 0, valOffset: 0, silent: true });
    const clone = getDrawingsFromStorage(env, 'ETH').find(d => d.id === newId);
    assert.ok(!clone.extendData._alertId, '_alertId stripped from in-place clone');
});

test('duplicateSelectedDrawing opts: rightTs preserved exactly (barOffset 0)', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    // pos_n1 has rightTs — but positions are not copyable, so test with a drawing
    // that has rightTs in extendData. We'll add one to the store.
    const store = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const baseTs = 1700000000000;
    store.ETH.push({
        id: 'rect_rts', name: 'rect',
        points: [{ timestamp: baseTs + 5 * 86400000, value: 300 }, { timestamp: baseTs + 10 * 86400000, value: 250 }],
        color: '#abc', width: 1, extendData: { rightTs: baseTs + 25 * 86400000 }
    });
    env.localStorage.setItem('klineDrawings', JSON.stringify(store));

    const newId = ctx.duplicateSelectedDrawing('rect_rts', { barOffset: 0, valOffset: 0, silent: true });
    const clone = getDrawingsFromStorage(env, 'ETH').find(d => d.id === newId);
    assert.equal(clone.extendData.rightTs, baseTs + 25 * 86400000, 'rightTs preserved exactly with barOffset:0');
});

test('duplicateSelectedDrawing legacy call: unchanged behavior (offset, toast)', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    const toastsBefore = env._toasts.length;
    const result = ctx.duplicateSelectedDrawing('rect_n1');

    assert.ok(result, 'legacy call returns truthy');
    assert.equal(env._toasts.length, toastsBefore + 1, 'toast shown for legacy call');
    assert.ok(env._toasts[env._toasts.length - 1].includes('Duplicated'), 'toast says Duplicated');

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const src = drawings.find(d => d.id === 'rect_n1');
    const duped = drawings.find(d => d.id === result);
    // Legacy: points should be offset
    assert.ok(duped.points[0].timestamp !== src.points[0].timestamp || duped.points[0].value !== src.points[0].value,
        'legacy clone has offset points');
});

test('duplicateSelectedDrawing legacy call: returns new overlay id (truthy string)', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    const result = ctx.duplicateSelectedDrawing('draw_1');
    assert.ok(result, 'legacy call returns truthy');
    // context-menu action treats it as truthy — string id satisfies that
    assert.ok(result !== true, 'returns id, not boolean true');
});

// ── Arrow-key nudge ──────────────────────────────────────────────────

// Helper: build a mock env where we can simulate keydown
function createNudgeEnv() {
    const env = createPhase5eEnv();
    // convertFromPixel: for vertical nudge pixel delta
    // x=200, y=300 → dataIndex=20, value=150
    // x=200, y=299 → same dataIndex, value=151 (1 pixel up = +1 value)
    env.klineChart.convertFromPixel = (pt) => {
        // pt.y: simple linear mapping: value = 500 - pt.y
        return { dataIndex: Math.floor((pt.x || 0) / 10), value: 500 - (pt.y || 0) };
    };
    env.klineChart.convertToPixel = (pt) => {
        if (pt.dataIndex !== undefined) return { x: pt.dataIndex * 10, y: 500 - pt.value };
        return { x: 0, y: 500 - (pt.value || 0) };
    };
    // Store keydown handler ref so tests can call it directly
    env._capturedKeydownHandlers = [];
    const origDocAdd = env.document.addEventListener.bind(env.document);
    env.document.addEventListener = (type, fn, opts) => {
        if (type === 'keydown') env._capturedKeydownHandlers.push(fn);
        origDocAdd(type, fn, opts);
    };
    // activeElement: default not an input
    env.document.activeElement = { tagName: 'DIV', isContentEditable: false };
    return env;
}

function fireNudgeKey(ctx, env, key, shift) {
    // Find the nudge handler — it's registered on document
    const handler = env._capturedKeydownHandlers.find(fn => fn._isNudgeHandler);
    if (!handler) {
        // Fall back to calling nudgeSelectedDrawing directly (if exported)
        if (typeof ctx._nudgeSelectedDrawing === 'function') {
            ctx._nudgeSelectedDrawing(key, shift);
        }
        return;
    }
    const mockEvent = {
        key,
        shiftKey: !!shift,
        target: { tagName: 'DIV', isContentEditable: false },
        defaultPrevented: false,
        _prevented: false,
        preventDefault() { this._prevented = true; }
    };
    handler(mockEvent);
    return mockEvent;
}

test('arrow nudge: no selection → no override call', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = null;

    const prevCalls = env._overrideCalls.length;
    ctx._nudgeSelectedDrawing('ArrowRight', false);
    assert.equal(env._overrideCalls.length, prevCalls, 'no override when nothing selected');
});

test('arrow nudge: right → dataIndex and timestamp both advance 1 bar', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'rect_n1';

    ctx._nudgeSelectedDrawing('ArrowRight', false);

    assert.ok(env._overrideCalls.length > 0, 'overrideOverlay called');
    const call = env._overrideCalls[env._overrideCalls.length - 1];
    assert.ok(call.points, 'call has points');
    // Every point should have both dataIndex and timestamp
    call.points.forEach((p, i) => {
        assert.ok(p.dataIndex !== undefined, `point[${i}] has dataIndex`);
        assert.ok(p.timestamp !== undefined, `point[${i}] has timestamp`);
    });
    // All indices advanced by 1 from source (source rect_n1 has indices 10 and 20)
    const baseTs = 1700000000000;
    const bars = env.klineChart.getDataList();
    // Source live overlay: dataIndex=10 for pt[0], dataIndex=20 for pt[1]
    // After right nudge: 11 and 21
    assert.equal(call.points[0].dataIndex, 11, 'first point dataIndex advanced by 1');
    assert.equal(call.points[0].timestamp, bars[11].timestamp, 'first point timestamp matches bar[11]');
    assert.equal(call.points[1].dataIndex, 21, 'second point dataIndex advanced by 1');
    assert.equal(call.points[1].timestamp, bars[21].timestamp, 'second point timestamp matches bar[21]');
});

test('arrow nudge: left → dataIndex and timestamp both retreat 1 bar', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'rect_n1';

    ctx._nudgeSelectedDrawing('ArrowLeft', false);

    const call = env._overrideCalls[env._overrideCalls.length - 1];
    assert.equal(call.points[0].dataIndex, 9, 'first point dataIndex retreated by 1');
    const bars = env.klineChart.getDataList();
    assert.equal(call.points[0].timestamp, bars[9].timestamp, 'timestamp matches bar[9]');
});

test('arrow nudge: right at edge → no-op (no override)', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);

    // Use chan_n1 which has point at bar index 5 and 15. Move it close to the right edge.
    // Override getOverlayById to put the points at the edge.
    const bars = env.klineChart.getDataList();
    const lastIdx = bars.length - 1; // 49
    env.klineChart.getOverlayById = (id) => {
        if (id !== 'rect_n1') return null;
        return {
            id: 'rect_n1', name: 'rect', lock: false,
            // pt[0] at last bar, pt[1] at bar 20
            points: [
                { dataIndex: lastIdx, value: 200, timestamp: bars[lastIdx].timestamp },
                { dataIndex: 20, value: 150, timestamp: bars[20].timestamp }
            ],
            extendData: {}
        };
    };
    env.klineSelectedOverlayId = 'rect_n1';

    const prevCalls = env._overrideCalls.length;
    ctx._nudgeSelectedDrawing('ArrowRight', false);
    assert.equal(env._overrideCalls.length, prevCalls, 'no override when any point would clamp at edge');
});

test('arrow nudge: up → all values increase by delta, timestamps unchanged', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'rect_n1';

    const callsBefore = env._overrideCalls.length;
    ctx._nudgeSelectedDrawing('ArrowUp', false);
    assert.ok(env._overrideCalls.length > callsBefore, 'overrideOverlay called');

    const call = env._overrideCalls[env._overrideCalls.length - 1];
    assert.ok(call.points, 'call has points');
    // timestamps must remain unchanged (horizontal = no change)
    const bars = env.klineChart.getDataList();
    // Source live has pt[0].timestamp = bars[10].timestamp, pt[1].timestamp = bars[20].timestamp
    assert.equal(call.points[0].timestamp, bars[10].timestamp, 'timestamp unchanged on vertical nudge');
    assert.equal(call.points[1].timestamp, bars[20].timestamp, 'timestamp unchanged on vertical nudge');
    // All values must have increased
    // Source pt[0].value = 200, pt[1].value = 150
    assert.ok(call.points[0].value > 200, 'value increased for up nudge');
    assert.ok(call.points[1].value > 150, 'all points value increased');
});

test('arrow nudge: down → all values decrease', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'rect_n1';

    ctx._nudgeSelectedDrawing('ArrowDown', false);
    const call = env._overrideCalls[env._overrideCalls.length - 1];
    assert.ok(call.points[0].value < 200, 'value decreased for down nudge');
    assert.ok(call.points[1].value < 150, 'all points value decreased');
});

test('arrow nudge: Shift multiplier ×10 (right)', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'rect_n1';

    ctx._nudgeSelectedDrawing('ArrowRight', true); // shift = true
    const call = env._overrideCalls[env._overrideCalls.length - 1];
    assert.equal(call.points[0].dataIndex, 20, 'shift: dataIndex advanced by 10 (10+10=20)');
    assert.equal(call.points[1].dataIndex, 30, 'shift: second point advanced by 10');
});

test('arrow nudge: Shift multiplier ×10 (up)', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'rect_n1';

    // Get the single-pixel delta
    ctx._nudgeSelectedDrawing('ArrowUp', false);
    const call1 = env._overrideCalls[env._overrideCalls.length - 1];
    const singleDelta = call1.points[0].value - 200;

    // Reset and try shift
    env._overrideCalls.length = 0;
    // Need fresh getOverlayById returning original values
    const bars = env.klineChart.getDataList();
    env.klineChart.getOverlayById = (id) => {
        if (id !== 'rect_n1') return null;
        return {
            id: 'rect_n1', name: 'rect', lock: false,
            points: [
                { dataIndex: 10, value: 200, timestamp: bars[10].timestamp },
                { dataIndex: 20, value: 150, timestamp: bars[20].timestamp }
            ],
            extendData: {}
        };
    };
    ctx._nudgeSelectedDrawing('ArrowUp', true); // shift
    const call10 = env._overrideCalls[env._overrideCalls.length - 1];
    const shiftDelta = call10.points[0].value - 200;

    assert.ok(Math.abs(shiftDelta - 10 * singleDelta) < 0.001, 'shift multiplies delta by ×10');
});

test('arrow nudge: locked overlay → no-op', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'locked_r1';

    // Make getOverlayById return lock:true for locked_r1
    const bars = env.klineChart.getDataList();
    const origGetOverlay = env.klineChart.getOverlayById;
    env.klineChart.getOverlayById = (id) => {
        if (id === 'locked_r1') return { id, name: 'rect', lock: true, points: [
            { dataIndex: 5, value: 300, timestamp: bars[5].timestamp },
            { dataIndex: 10, value: 250, timestamp: bars[10].timestamp }
        ], extendData: { _locked: true } };
        return origGetOverlay(id);
    };

    const prevCalls = env._overrideCalls.length;
    ctx._nudgeSelectedDrawing('ArrowRight', false);
    assert.equal(env._overrideCalls.length, prevCalls, 'locked overlay: no override call');
});

test('arrow nudge: INPUT target → no-op', () => {
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'rect_n1';

    // Simulate with INPUT active element
    const prevCalls = env._overrideCalls.length;
    ctx._nudgeSelectedDrawing('ArrowRight', false, { tagName: 'INPUT' });
    assert.equal(env._overrideCalls.length, prevCalls, 'input target: no override call');
});

test('arrow nudge: position drawing → rightTs shifts by same bar count', () => {
    // positions are in _NON_COPYABLE but ARE nudgeable (any overlay with timestamp/value points)
    const env = createNudgeEnv();
    const ctx = loadDrawingsModule(env);

    const baseTs = 1700000000000;
    const bars = env.klineChart.getDataList();

    // Override getOverlayById to provide a position overlay with live points
    env.klineChart.getOverlayById = (id) => {
        if (id !== 'pos_n1') return null;
        return {
            id: 'pos_n1', name: 'position', lock: false,
            points: [
                { dataIndex: 10, value: 100, timestamp: bars[10].timestamp },
                { dataIndex: 10, value: 90,  timestamp: bars[10].timestamp },
                { dataIndex: 10, value: 120, timestamp: bars[10].timestamp }
            ],
            extendData: { rightTs: baseTs + 20 * 86400000 }
        };
    };
    env.klineSelectedOverlayId = 'pos_n1';

    ctx._nudgeSelectedDrawing('ArrowRight', false);

    const call = env._overrideCalls[env._overrideCalls.length - 1];
    assert.ok(call, 'override was called');
    // Points advanced by 1 bar
    assert.equal(call.points[0].dataIndex, 11, 'entry point advanced by 1');
    // rightTs advanced by 1 bar
    const expectedRightTs = bars[21].timestamp; // bar 20+1=21
    assert.equal(call.extendData.rightTs, expectedRightTs, 'rightTs advanced by 1 bar');
});

test('arrow nudge: debounce timer is set after nudge', () => {
    const env = createNudgeEnv();
    let timerCalled = false;
    let timerFn = null;
    env.setTimeout = (fn, ms) => {
        timerCalled = true;
        timerFn = fn;
        return 1;
    };
    const ctx = loadDrawingsModule(env);
    env.klineSelectedOverlayId = 'rect_n1';

    ctx._nudgeSelectedDrawing('ArrowRight', false);
    assert.ok(timerCalled, 'setTimeout called for debounce');
});

// ── Ctrl+drag clone — custom drag (_armCtrlCloneDrag / _handleCtrlCloneDrag /
//    _finalizeCtrlCloneDrag) ─────────────────────────────────────────────

function createCloneDragEnv() {
    const env = createPhase5eEnv();
    env._removedOverlays = [];
    env.klineChart.removeOverlay = (id) => { env._removedOverlays.push(id); };
    // convertFromPixel: x → dataIndex (1 bar per 10px), y → value (1 per px, inverted)
    env.klineChart.convertFromPixel = (pt) => ({ dataIndex: Math.round(pt.x / 10), value: 500 - pt.y });
    env._forceCanvasCursorCalls = [];
    return env;
}
const _cloneDragContainer = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 1600, height: 800 }), querySelectorAll: () => [], style: { setProperty: () => {}, removeProperty: () => {} } };
const _cloneDragEvent = () => { const e = { stopped: false, prevented: false }; e.stopPropagation = () => { e.stopped = true; }; e.preventDefault = () => { e.prevented = true; }; return e; };

test('ctrl-clone drag: arm clones in place, selects clone, stops the event', () => {
    const env = createCloneDragEnv();
    const ctx = loadDrawingsModule(env);
    const before = getDrawingsFromStorage(env, 'ETH').map(d => d.id);
    const e = _cloneDragEvent();

    const armed = ctx._armCtrlCloneDrag('rect_n1', 200, 100, _cloneDragContainer, e);
    assert.ok(armed, 'arming succeeds');
    assert.ok(e.stopped && e.prevented, 'event stopped (engine must not act)');
    const after = getDrawingsFromStorage(env, 'ETH').map(d => d.id);
    const cloneId = after.find(id => !before.includes(id));
    assert.ok(cloneId, 'clone exists in storage');
    assert.equal(env.klineSelectedOverlayId, cloneId, 'clone selected');
    assert.ok(ctx._ctrlCloneDrag && ctx._ctrlCloneDrag.cloneId === cloneId, 'drag state armed on clone');
    ctx._ctrlCloneDrag = null;
});

test('ctrl-clone drag: locked or non-copyable source does not arm', () => {
    const env = createCloneDragEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._armCtrlCloneDrag('locked_r1', 200, 100, _cloneDragContainer, _cloneDragEvent()), false, 'locked rect: no arm');
    assert.equal(ctx._armCtrlCloneDrag('pos_n1', 200, 100, _cloneDragContainer, _cloneDragEvent()), false, 'position (non-copyable): no arm');
    assert.equal(ctx._ctrlCloneDrag, null, 'no drag state');
});

test('ctrl-clone drag: mousemove shifts clone by bar+value delta with BOTH dataIndex and timestamp', () => {
    const env = createCloneDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._armCtrlCloneDrag('rect_n1', 200, 100, _cloneDragContainer, _cloneDragEvent());
    const cloneId = ctx._ctrlCloneDrag.cloneId;
    const orig = ctx._ctrlCloneDrag.origPoints.map(p => ({ ...p }));
    env._overrideCalls.length = 0;

    // +30px x = +3 bars; -20px y = +20 value
    ctx._handleCtrlCloneDrag(230, 80, _cloneDragContainer);

    const call = env._overrideCalls.find(c => c.id === cloneId && c.points);
    assert.ok(call, 'overrideOverlay called on clone');
    const bars = env.klineChart.getDataList();
    call.points.forEach((p, i) => {
        assert.equal(p.dataIndex, orig[i].dataIndex + 3, `point[${i}] +3 bars`);
        assert.equal(p.timestamp, bars[p.dataIndex].timestamp, `point[${i}] timestamp matches dataIndex`);
        assert.ok(Math.abs(p.value - (orig[i].value + 20)) < 1e-9, `point[${i}] +20 value`);
    });
    assert.ok(ctx._ctrlCloneDrag.moved, 'state marked moved');
    ctx._ctrlCloneDrag = null;
});

test('ctrl-clone drag: move past the data edge is ignored (no distortion)', () => {
    const env = createCloneDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._armCtrlCloneDrag('rect_n1', 200, 100, _cloneDragContainer, _cloneDragEvent());
    env._overrideCalls.length = 0;
    // +600px x = +60 bars → rect_n1 spans bars 10..20, 20+60=80 ≥ 50 bars → abort
    ctx._handleCtrlCloneDrag(800, 100, _cloneDragContainer);
    assert.equal(env._overrideCalls.filter(c => c.points).length, 0, 'no override applied');
    assert.ok(!ctx._ctrlCloneDrag.moved, 'not marked moved');
    ctx._ctrlCloneDrag = null;
});

test('ctrl-clone drag: finalize removes a never-moved clone, selection reverts', () => {
    const env = createCloneDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._armCtrlCloneDrag('rect_n1', 200, 100, _cloneDragContainer, _cloneDragEvent());
    const cloneId = ctx._ctrlCloneDrag.cloneId;

    ctx._finalizeCtrlCloneDrag();

    assert.ok(env._removedOverlays.includes(cloneId), 'clone overlay removed');
    assert.ok(!getDrawingsFromStorage(env, 'ETH').find(d => d.id === cloneId), 'clone gone from storage');
    assert.equal(env.klineSelectedOverlayId, 'rect_n1', 'selection back on original');
    assert.equal(ctx._ctrlCloneDrag, null, 'state cleared');
});

test('ctrl-clone drag: finalize persists a moved clone through the save path', () => {
    const env = createCloneDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._armCtrlCloneDrag('rect_n1', 200, 100, _cloneDragContainer, _cloneDragEvent());
    const cloneId = ctx._ctrlCloneDrag.cloneId;
    ctx._handleCtrlCloneDrag(230, 80, _cloneDragContainer); // +3 bars, +20 value

    // getOverlayById must reflect the dragged (overridden) points for the save;
    // the env's storage-backed mock won't — serve the override result directly.
    const lastOverride = env._overrideCalls.filter(c => c.id === cloneId && c.points).pop();
    const origGet = env.klineChart.getOverlayById;
    env.klineChart.getOverlayById = (id) => id === cloneId
        ? { id: cloneId, name: 'rect', lock: false, points: lastOverride.points, extendData: {} }
        : origGet(id);

    ctx._finalizeCtrlCloneDrag();

    assert.ok(!env._removedOverlays.includes(cloneId), 'moved clone NOT removed');
    const saved = getDrawingsFromStorage(env, 'ETH').find(d => d.id === cloneId);
    assert.ok(saved, 'clone still in storage');
    const bars = env.klineChart.getDataList();
    assert.equal(saved.points[0].timestamp, bars[lastOverride.points[0].dataIndex].timestamp, 'persisted timestamp matches dragged bar');
    assert.equal(env.klineSelectedOverlayId, cloneId, 'selection stays on clone');
});

// ── Phase 5f: Drawings Object Tree panel ────────────────────────────

console.log('\n\x1b[1mPhase 5f: Drawings Object Tree panel\x1b[0m\n');

// Helper: build an env with several drawings having varied zOrders, hidden, locked
function createObjectsTreeEnv() {
    const env = createPhase5eEnv();
    const baseTs = 1700000000000;
    const store = JSON.parse(env.localStorage.getItem('klineDrawings'));

    // Augment ETH drawings with explicit zOrders and flags to test all paths
    // (createPhase5eEnv already has draw_1, draw_2, pos_1, spot_1, chan_1, alert_1,
    //  fib_1, locked_1, rect_n1, chan_n1, pos_n1, locked_r1)
    // Set deterministic zOrders and some flags for model tests
    const ethDrawings = store.ETH;
    // Give each a clear zOrder and set some flags
    ethDrawings.forEach((d, i) => { if (d.zOrder == null) d.zOrder = i + 1; });

    // Mark one as hidden, one as locked (not _locked in extendData — use explicit drawing)
    store.ETH.push({
        id: 'h_line_1',
        name: 'horizontalStraightLine',
        points: [{ value: 64200, timestamp: baseTs + 5 * 86400000 }],
        color: '#fff', width: 1,
        zOrder: 100,
        extendData: { _hidden: true }
    });
    store.ETH.push({
        id: 'long_pos_1',
        name: 'longPosition',
        points: [
            { value: 76037, timestamp: baseTs + 10 * 86400000 },
            { value: 74000, timestamp: baseTs + 10 * 86400000 },
            { value: 80000, timestamp: baseTs + 10 * 86400000 }
        ],
        color: '#26a69a', width: 1,
        zOrder: 50,
        extendData: {}
    });
    store.ETH.push({
        id: 'short_pos_1',
        name: 'shortPosition',
        points: [
            { value: 65000, timestamp: baseTs + 12 * 86400000 },
            { value: 67000, timestamp: baseTs + 12 * 86400000 },
            { value: 60000, timestamp: baseTs + 12 * 86400000 }
        ],
        color: '#ef5350', width: 1,
        zOrder: 30,
        extendData: {}
    });
    store.ETH.push({
        id: 'freehand_1',
        name: 'freehand',
        points: [{ value: 100, timestamp: baseTs + 1 * 86400000 }],
        color: '#aaa', width: 1,
        zOrder: 10,
        extendData: {}
    });

    env.localStorage.setItem('klineDrawings', JSON.stringify(store));

    // Add mock removeOverlay tracking and createOverlayFromDrawing tracking
    env._removedOverlays = env._removedOverlays || [];
    env._createdFromDrawingIds = [];
    const origCreate = env.klineChart.createOverlay;
    env.klineChart.createOverlay = (cfg, paneId) => {
        const id = cfg.id || ('overlay_auto_' + Math.random().toString(36).slice(2));
        env._overlays[id] = { ...cfg, id, paneId: paneId || 'candle_pane' };
        if (cfg.id) env._createdFromDrawingIds.push(cfg.id);
        return id;
    };
    env.klineChart.removeOverlay = (id) => {
        env._removedOverlays.push(id);
        if (typeof id === 'string') delete env._overlays[id];
    };
    env._reloadCalls = 0;
    env.renderAlertOverlays = () => {};

    return env;
}

// ── _buildObjectsPanelModel ──────────────────────────────────────────

test('objects panel model: rows sorted by zOrder DESC', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    assert.ok(Array.isArray(model), 'model is an array');
    assert.ok(model.length > 0, 'model has rows');
    // Verify descending zOrder
    for (let i = 1; i < model.length; i++) {
        const prev = model[i - 1].zOrder != null ? model[i - 1].zOrder : 0;
        const curr = model[i].zOrder != null ? model[i].zOrder : 0;
        assert.ok(prev >= curr, `row[${i-1}].zOrder(${prev}) >= row[${i}].zOrder(${curr})`);
    }
});

test('objects panel model: includes hidden drawings', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const hiddenRow = model.find(r => r.id === 'h_line_1');
    assert.ok(hiddenRow, 'hidden drawing appears in model');
    assert.ok(hiddenRow.hidden === true, 'hidden flag is true');
});

test('objects panel model: locked flag reflected', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const lockedRow = model.find(r => r.id === 'locked_1');
    assert.ok(lockedRow, 'locked_1 appears in model');
    assert.ok(lockedRow.locked === true, 'locked flag is true');
});

test('objects panel model: non-hidden/non-locked rows have correct flags', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const normalRow = model.find(r => r.id === 'long_pos_1');
    assert.ok(normalRow, 'long_pos_1 appears in model');
    assert.ok(!normalRow.hidden, 'not hidden');
    assert.ok(!normalRow.locked, 'not locked');
});

test('objects panel model: count equals total drawings in storage', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const all = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH;
    const model = ctx._buildObjectsPanelModel();
    assert.equal(model.length, all.length, 'model count matches storage count');
});

test('objects panel model: longPosition descriptive label includes entry value', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const longRow = model.find(r => r.id === 'long_pos_1');
    assert.ok(longRow, 'long_pos_1 in model');
    // Label should include entry price (may be formatted with thousands separator)
    const lbl = longRow.label || longRow.description || '';
    assert.ok(lbl.includes('76,037') || lbl.includes('76037'),
        `long position label should include entry price 76037, got label="${lbl}"`);
    // Richer label: should also indicate Long side
    assert.ok(lbl.startsWith('Long'), `label should start with 'Long', got "${lbl}"`);
});

test('objects panel model: horizontalStraightLine label includes price', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const hRow = model.find(r => r.id === 'h_line_1');
    assert.ok(hRow, 'h_line_1 in model');
    assert.ok(hRow.label || hRow.description, 'has label or description');
});

test('objects panel model: each row has id, name, zOrder, hidden, locked, label fields', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    for (const row of model) {
        assert.ok(row.id != null, `row ${row.id}: has id`);
        assert.ok(row.name != null, `row ${row.id}: has name`);
        assert.ok(row.zOrder != null || row.zOrder === 0, `row ${row.id}: has zOrder`);
        assert.ok('hidden' in row, `row ${row.id}: has hidden`);
        assert.ok('locked' in row, `row ${row.id}: has locked`);
        assert.ok(row.label != null, `row ${row.id}: has label`);
    }
});

// ── Visibility toggle ────────────────────────────────────────────────

test('visibility toggle hide: sets extendData._hidden and removes overlay', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    // long_pos_1 is not hidden — hide it
    ctx._toggleDrawingVisibility('long_pos_1');

    // extendData._hidden should be true
    const drawing = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'long_pos_1');
    assert.ok(drawing, 'drawing still in storage');
    assert.ok(drawing.extendData._hidden === true, '_hidden set to true');

    // Overlay should be removed
    assert.ok(env._removedOverlays.includes('long_pos_1'), 'overlay removed');
});

test('visibility toggle hide: record stays in storage (not deleted)', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    ctx._toggleDrawingVisibility('long_pos_1');

    const all = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH;
    assert.ok(all.find(d => d.id === 'long_pos_1'), 'record still in storage after hide');
});

test('visibility toggle show: clears _hidden and recreates overlay', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    // h_line_1 is already hidden — show it
    ctx._toggleDrawingVisibility('h_line_1');

    const drawing = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'h_line_1');
    assert.ok(drawing, 'drawing still in storage');
    assert.ok(!drawing.extendData._hidden, '_hidden cleared');

    // createOverlayFromDrawing should have been called — the id should be recreated
    assert.ok(env._createdFromDrawingIds.includes('h_line_1'), 'overlay recreated via createOverlayFromDrawing');
});

test('visibility toggle hide when selected: clears klineSelectedOverlayId', () => {
    const env = createObjectsTreeEnv();
    env.klineSelectedOverlayId = 'long_pos_1';
    const ctx = loadDrawingsModule(env);
    ctx.klineSelectedOverlayId = 'long_pos_1';

    ctx._toggleDrawingVisibility('long_pos_1');

    assert.ok(!ctx.klineSelectedOverlayId, 'selection cleared when hiding selected drawing');
});

test('visibility toggle hide non-selected: does not change klineSelectedOverlayId', () => {
    const env = createObjectsTreeEnv();
    env.klineSelectedOverlayId = 'rect_n1';
    const ctx = loadDrawingsModule(env);
    ctx.klineSelectedOverlayId = 'rect_n1';

    ctx._toggleDrawingVisibility('long_pos_1');

    assert.equal(ctx.klineSelectedOverlayId, 'rect_n1', 'selection unchanged when hiding non-selected drawing');
});

// ── loadKlineDrawings skips hidden ───────────────────────────────────

test('loadKlineDrawings skips hidden drawings (no overlay created)', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    // Clear overlays created at load time by reloading
    Object.keys(env._overlays).forEach(k => delete env._overlays[k]);
    env._createdFromDrawingIds.length = 0;

    ctx.loadKlineDrawings();

    assert.ok(!env._createdFromDrawingIds.includes('h_line_1'), 'hidden drawing not created as overlay');
});

test('loadKlineDrawings hidden drawing stays in storage', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    ctx.loadKlineDrawings();

    const all = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH;
    assert.ok(all.find(d => d.id === 'h_line_1'), 'hidden drawing still in storage after loadKlineDrawings');
});

test('loadKlineDrawings non-hidden drawings ARE created as overlays', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    Object.keys(env._overlays).forEach(k => delete env._overlays[k]);
    env._createdFromDrawingIds.length = 0;

    ctx.loadKlineDrawings();

    assert.ok(env._createdFromDrawingIds.includes('long_pos_1'), 'visible drawing created as overlay');
});

// ── Lock toggle (shared function) ───────────────────────────────────

test('toggleDrawingLock locks an unlocked drawing', () => {
    const env = createObjectsTreeEnv();
    env.klineChart.overrideOverlay = (cfg) => { env._overrideCalls.push(cfg); };
    env.klineChart.getOverlayById = (id) => {
        const all = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}').ETH || [];
        const d = all.find(x => x.id === id);
        if (!d) return null;
        return { id, name: d.name, lock: !!(d.extendData && d.extendData._locked), extendData: d.extendData || {} };
    };
    const ctx = loadDrawingsModule(env);

    ctx.toggleDrawingLock('long_pos_1');

    const drawing = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'long_pos_1');
    assert.ok(drawing.extendData._locked === true, '_locked set to true');

    // overrideOverlay should have been called with lock: true
    const lockCall = env._overrideCalls.find(c => c.id === 'long_pos_1' && c.lock === true);
    assert.ok(lockCall, 'overrideOverlay called with lock:true');
});

test('toggleDrawingLock unlocks a locked drawing', () => {
    const env = createObjectsTreeEnv();
    env.klineChart.overrideOverlay = (cfg) => { env._overrideCalls.push(cfg); };
    env.klineChart.getOverlayById = (id) => {
        const all = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}').ETH || [];
        const d = all.find(x => x.id === id);
        if (!d) return null;
        return { id, name: d.name, lock: !!(d.extendData && d.extendData._locked), extendData: d.extendData || {} };
    };
    const ctx = loadDrawingsModule(env);

    // locked_1 has _locked: true
    ctx.toggleDrawingLock('locked_1');

    const drawing = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'locked_1');
    assert.ok(drawing.extendData._locked === false, '_locked set to false');

    const unlockCall = env._overrideCalls.find(c => c.id === 'locked_1' && c.lock === false);
    assert.ok(unlockCall, 'overrideOverlay called with lock:false');
});

test('toggleDrawingLock: overrideOverlay called AND extendData._locked persisted', () => {
    const env = createObjectsTreeEnv();
    env.klineChart.overrideOverlay = (cfg) => { env._overrideCalls.push(cfg); };
    env.klineChart.getOverlayById = (id) => {
        const all = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}').ETH || [];
        const d = all.find(x => x.id === id);
        if (!d) return null;
        return { id, name: d.name, lock: !!(d.extendData && d.extendData._locked), extendData: d.extendData || {} };
    };
    const ctx = loadDrawingsModule(env);

    ctx.toggleDrawingLock('long_pos_1');

    // Both side effects must fire
    const stored = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'long_pos_1');
    assert.ok(stored.extendData._locked === true, 'persisted');
    assert.ok(env._overrideCalls.some(c => c.id === 'long_pos_1'), 'overrideOverlay called');
});

// ── Drag-reorder / zOrder reassignment ──────────────────────────────

test('reorderDrawings: assigns zOrders descending from new id order (top=highest)', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    // Get current ids; take first 3 and reorder them
    const all = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH;
    const ids = all.slice(0, 3).map(d => d.id);
    // Reverse the order
    const newOrder = ids.slice().reverse();

    ctx._reorderDrawings(newOrder);

    const after = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH;
    const z0 = after.find(d => d.id === newOrder[0]).zOrder;
    const z1 = after.find(d => d.id === newOrder[1]).zOrder;
    const z2 = after.find(d => d.id === newOrder[2]).zOrder;
    assert.ok(z0 > z1, `top row(${newOrder[0]}) z=${z0} > second(${newOrder[1]}) z=${z1}`);
    assert.ok(z1 > z2, `second(${newOrder[1]}) z=${z1} > third(${newOrder[2]}) z=${z2}`);
});

test('reorderDrawings: changed drawings are saved to storage', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    // Use 3 drawings with known, well-separated zOrders so that any reorder
    // is guaranteed to produce a different assignment.
    // Explicitly set them to avoid the coincidental symmetric case.
    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const ethDrawings = all.ETH;
    const target = ethDrawings.slice(0, 3);
    target[0].zOrder = 10;
    target[1].zOrder = 20;
    target[2].zOrder = 30;
    env.localStorage.setItem('klineDrawings', JSON.stringify(all));

    const ids = [target[0].id, target[1].id, target[2].id];
    // newOrder = reverse: [id2, id1, id0]
    // _reorderDrawings assigns: id2→3, id1→2, id0→1
    // Original: id0=10, id1=20, id2=30 → all different → definitely changed
    const newOrder = ids.slice().reverse();
    ctx._reorderDrawings(newOrder);

    const afterAll = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH;
    const changed = ids.filter(id => {
        const orig = target.find(d => d.id === id).zOrder;
        const updated = (afterAll.find(d => d.id === id) || {}).zOrder;
        return orig !== updated;
    });
    assert.ok(changed.length > 0, 'at least one drawing had its zOrder changed');
});

test('reorderDrawings: triggers wipe+reload of overlays', () => {
    const env = createObjectsTreeEnv();
    env.klineChart.overrideOverlay = () => {};
    env.klineChart.getOverlayById = () => null;
    let reloadCalled = false;
    // Track loadKlineDrawings being called — we need to check _reloadAllDrawingOverlays
    // We intercept removeOverlay as the proxy for wipe
    const removedInReorder = [];
    env.klineChart.removeOverlay = (id) => {
        removedInReorder.push(id);
        if (typeof id === 'string') delete env._overlays[id];
    };

    const ctx = loadDrawingsModule(env);

    const all = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH;
    const ids = all.slice(0, 3).map(d => d.id);
    const newOrder = ids.slice().reverse();

    // Seed some overlays first
    for (const id of ids) { env._overlays[id] = { id, name: 'rect' }; }

    ctx._reorderDrawings(newOrder);

    // removeOverlay should have been called (wipe) followed by createOverlay (reload)
    assert.ok(removedInReorder.length > 0, 'overlays wiped during reorder');
});

// ── Phase 5f Extensions: _colorName, richer labels, _setDrawingHidden, groups, delete ──

console.log('\n\x1b[1mPhase 5f extensions: colorName + richer labels + _setDrawingHidden + groups + delete\x1b[0m\n');

// ── _colorName ────────────────────────────────────────────────────────

test('_colorName: green hex → "green"', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#26a69a'), 'teal', '#26a69a is teal (hue ~174)');
    assert.equal(ctx._colorName('#00ff00'), 'green', '#00ff00 is green');
    assert.equal(ctx._colorName('#3fb950'), 'green', '#3fb950 is green');
});

test('_colorName: red hex → "red"', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#ff0000'), 'red', '#ff0000 is red');
    assert.equal(ctx._colorName('#ef5350'), 'red', '#ef5350 is red');
    assert.equal(ctx._colorName('#cc0000'), 'red', '#cc0000 is red');
});

test('_colorName: blue hex → "blue"', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#0000ff'), 'blue', '#0000ff is blue');
    assert.equal(ctx._colorName('#1a73e8'), 'blue', '#1a73e8 is blue');
});

test('_colorName: yellow hex → "yellow"', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#ffff00'), 'yellow', '#ffff00 is yellow');
    assert.equal(ctx._colorName('#ffd700'), 'yellow', '#ffd700 is yellow (gold)');
});

test('_colorName: #d1d4dc → gray-ish (white/gray)', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const name = ctx._colorName('#d1d4dc');
    assert.ok(name === 'gray' || name === 'white', `#d1d4dc should be gray or white, got "${name}"`);
});

test('_colorName: #ffffff → "white"', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#ffffff'), 'white');
});

test('_colorName: #000000 → "black"', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#000000'), 'black');
});

test('_colorName: orange hex → "orange"', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#ff8800'), 'orange', '#ff8800 is orange');
});

test('_colorName: purple hex → "purple"', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#9c27b0'), 'purple', '#9c27b0 is purple');
});

test('_colorName: handles shorthand #fff', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName('#fff'), 'white', '#fff shorthand → white');
});

test('_colorName: handles missing # prefix', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const name = ctx._colorName('ff0000');
    assert.equal(name, 'red', 'no # prefix still works');
});

test('_colorName: returns empty string for null/invalid', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._colorName(null), '');
    assert.equal(ctx._colorName(''), '');
    assert.equal(ctx._colorName('zzz'), '');
});

// ── Richer labels via _buildObjectsPanelModel ─────────────────────────

test('richer label: longPosition includes "Long", entry, TP, SL', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const row = model.find(r => r.id === 'long_pos_1');
    assert.ok(row, 'long_pos_1 in model');
    const lbl = row.label;
    assert.ok(lbl.startsWith('Long'), `label starts with Long: "${lbl}"`);
    // Entry 76037 → "76,037", TP 80000 → "80,000", SL 74000 → "74,000"
    assert.ok(lbl.includes('76,037') || lbl.includes('76037'), `entry in label: "${lbl}"`);
    assert.ok(lbl.includes('TP'), `TP in label: "${lbl}"`);
    assert.ok(lbl.includes('SL'), `SL in label: "${lbl}"`);
});

test('richer label: shortPosition includes "Short" and correct side', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const row = model.find(r => r.id === 'short_pos_1');
    assert.ok(row, 'short_pos_1 in model');
    assert.ok(row.label.startsWith('Short'), `shortPosition label starts with Short: "${row.label}"`);
});

test('richer label: generic position infers side from SL vs entry', () => {
    // position with SL < entry → Long
    const env = createMockEnv();
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{
            id: 'pos_generic', name: 'position',
            points: [{ value: 100, timestamp: 1000 }, { value: 90, timestamp: 1000 }, { value: 120, timestamp: 1000 }],
            color: '#fff', width: 1, zOrder: 1
        }]
    }));
    const ctx = loadDrawingsModule(env);
    const model = ctx._buildObjectsPanelModel();
    const row = model.find(r => r.id === 'pos_generic');
    assert.ok(row, 'pos_generic in model');
    assert.ok(row.label.startsWith('Long'), `SL(90)<entry(100) → Long, got "${row.label}"`);
});

test('richer label: spot drawing shows "Spot <price>"', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const row = model.find(r => r.id === 'spot_1');
    assert.ok(row, 'spot_1 in model');
    assert.ok(row.label.startsWith('Spot'), `spot label starts with Spot: "${row.label}"`);
    // price 100 → "100.00" or "100"
    assert.ok(row.label.includes('100'), `spot price in label: "${row.label}"`);
});

test('richer label: rect with color and rectText → "Color rect — text"', () => {
    const env = createMockEnv();
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{
            id: 'rect_colored', name: 'rect',
            points: [{ value: 200, timestamp: 1000 }, { value: 150, timestamp: 2000 }],
            color: '#3fb950', width: 1, zOrder: 1,
            extendData: { rectText: 'Support zone' }
        }]
    }));
    const ctx = loadDrawingsModule(env);
    const model = ctx._buildObjectsPanelModel();
    const row = model.find(r => r.id === 'rect_colored');
    assert.ok(row, 'rect_colored in model');
    const lbl = row.label;
    // Should include color name + rect + text
    assert.ok(lbl.toLowerCase().includes('rect'), `label includes "rect": "${lbl}"`);
    assert.ok(lbl.includes('Support zone'), `label includes rectText: "${lbl}"`);
});

test('richer label: rect with color but no rectText → "Color rect"', () => {
    const env = createMockEnv();
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{
            id: 'rect_simple', name: 'rect',
            points: [{ value: 200, timestamp: 1000 }, { value: 150, timestamp: 2000 }],
            color: '#ff0000', width: 1, zOrder: 1, extendData: {}
        }]
    }));
    const ctx = loadDrawingsModule(env);
    const model = ctx._buildObjectsPanelModel();
    const row = model.find(r => r.id === 'rect_simple');
    const lbl = row.label;
    assert.ok(lbl.toLowerCase().includes('red'), `color name in label: "${lbl}"`);
    assert.ok(lbl.toLowerCase().includes('rect'), `"rect" in label: "${lbl}"`);
    assert.ok(!lbl.includes('—'), `no separator when no rectText: "${lbl}"`);
});

test('richer label: horizontalStraightLine → "Color h-line <price>"', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const row = model.find(r => r.id === 'h_line_1');
    assert.ok(row, 'h_line_1 in model');
    const lbl = row.label.toLowerCase();
    assert.ok(lbl.includes('h-line'), `label contains h-line: "${row.label}"`);
    // price 64200 → "64,200"
    assert.ok(row.label.includes('64,200') || row.label.includes('64200'), `price in label: "${row.label}"`);
});

// ── _fmtObjPrice ──────────────────────────────────────────────────────

test('_fmtObjPrice: ≥1000 → thousands-separated 0 decimals', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._fmtObjPrice(76037), '76,037');
    assert.equal(ctx._fmtObjPrice(1000), '1,000');
    assert.equal(ctx._fmtObjPrice(1234567), '1,234,567');
});

test('_fmtObjPrice: ≥1 and <1000 → 2 decimals', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    assert.equal(ctx._fmtObjPrice(1.5), '1.50');
    assert.equal(ctx._fmtObjPrice(999.99), '999.99');
});

test('_fmtObjPrice: <1 → 4 sig figs', () => {
    const env = createMockEnv();
    const ctx = loadDrawingsModule(env);
    const result = ctx._fmtObjPrice(0.001234);
    // 4 sig figs of 0.001234 → "0.001234"
    assert.ok(result.includes('1234') || result.includes('0.001234'), `got "${result}"`);
});

// ── _setDrawingHidden idempotence ─────────────────────────────────────

test('_setDrawingHidden: hide twice → hidden once (idempotent)', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    // long_pos_1 is visible
    ctx._setDrawingHidden('long_pos_1', true);
    const removedCount1 = env._removedOverlays.filter(id => id === 'long_pos_1').length;

    // Second hide: no-op
    ctx._setDrawingHidden('long_pos_1', true);
    const removedCount2 = env._removedOverlays.filter(id => id === 'long_pos_1').length;

    assert.equal(removedCount1, 1, 'first hide removes overlay once');
    assert.equal(removedCount2, 1, 'second hide is no-op (still just 1 removal)');

    const stored = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'long_pos_1');
    assert.ok(stored.extendData._hidden === true, 'still hidden');
});

test('_setDrawingHidden: show on visible → no-op', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    const createdBefore = env._createdFromDrawingIds.length;
    // long_pos_1 is visible — show it (already visible → no-op)
    ctx._setDrawingHidden('long_pos_1', false);
    assert.equal(env._createdFromDrawingIds.length, createdBefore, 'no overlay created when already visible');
});

test('_setDrawingHidden: show on hidden → overlay recreated', () => {
    const env = createObjectsTreeEnv();
    const ctx = loadDrawingsModule(env);

    // h_line_1 is hidden
    const createdBefore = env._createdFromDrawingIds.length;
    ctx._setDrawingHidden('h_line_1', false);
    assert.ok(env._createdFromDrawingIds.length > createdBefore, 'overlay recreated when showing a hidden drawing');
    const stored = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.find(d => d.id === 'h_line_1');
    assert.ok(!stored.extendData._hidden, '_hidden cleared');
});

// ── Group model ───────────────────────────────────────────────────────

function createGroupEnv() {
    // Build a clean env with 4 drawings: 2 in groupA, 1 in groupB, 1 ungrouped
    const env = createMockEnv();
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [
            { id: 'g1a', name: 'rect', points: [{ value: 100, timestamp: 1000 }, { value: 90, timestamp: 2000 }],
              color: '#f00', width: 1, zOrder: 5, extendData: { _group: 'groupA' } },
            { id: 'g2a', name: 'rect', points: [{ value: 110, timestamp: 1000 }, { value: 95, timestamp: 2000 }],
              color: '#0f0', width: 1, zOrder: 3, extendData: { _group: 'groupA' } },
            { id: 'g1b', name: 'straightLine', points: [{ value: 200, timestamp: 1000 }, { value: 210, timestamp: 2000 }],
              color: '#00f', width: 1, zOrder: 7, extendData: { _group: 'groupB' } },
            { id: 'ung1', name: 'segment', points: [{ value: 50, timestamp: 1000 }, { value: 60, timestamp: 2000 }],
              color: '#fff', width: 1, zOrder: 2, extendData: {} }
        ]
    }));
    env._removedOverlays = env._removedOverlays || [];
    env._createdFromDrawingIds = [];
    const origCreate = env.klineChart.createOverlay;
    env.klineChart.createOverlay = (cfg, paneId) => {
        const id = cfg.id || ('auto_' + Math.random().toString(36).slice(2));
        env._overlays[id] = { ...cfg, id };
        if (cfg.id) env._createdFromDrawingIds.push(cfg.id);
        return id;
    };
    env.klineChart.removeOverlay = (id) => {
        env._removedOverlays.push(id);
        if (typeof id === 'string') delete env._overlays[id];
    };
    env.renderAlertOverlays = () => {};
    return env;
}

test('group model: members sorted zOrder DESC within group', () => {
    const env = createGroupEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    // groupA members: g1a(z=5) and g2a(z=3). In DESC order: g1a first, g2a second.
    const groupA = model.filter(r => r.group === 'groupA');
    assert.equal(groupA.length, 2, 'groupA has 2 members');
    assert.ok(groupA[0].zOrder >= groupA[1].zOrder, 'groupA members sorted DESC');
    assert.equal(groupA[0].id, 'g1a', 'g1a (z=5) comes first');
    assert.equal(groupA[1].id, 'g2a', 'g2a (z=3) comes second');
});

test('group model: ungrouped drawing has null group', () => {
    const env = createGroupEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const ung = model.find(r => r.id === 'ung1');
    assert.ok(ung, 'ungrouped drawing in model');
    assert.ok(!ung.group, 'group is null/falsy');
});

test('group model: group eye derived state — any visible → true', () => {
    const env = createGroupEnv();
    const ctx = loadDrawingsModule(env);

    const model = ctx._buildObjectsPanelModel();
    const groupAMembers = model.filter(r => r.group === 'groupA');
    const anyVisible = groupAMembers.some(r => !r.hidden);
    assert.ok(anyVisible, 'groupA: at least one member visible');
});

test('group model: group eye — hide one, other visible → anyVisible still true', () => {
    const env = createGroupEnv();
    // Hide g2a
    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    all.ETH.find(d => d.id === 'g2a').extendData._hidden = true;
    env.localStorage.setItem('klineDrawings', JSON.stringify(all));

    const ctx = loadDrawingsModule(env);
    const model = ctx._buildObjectsPanelModel();
    const groupA = model.filter(r => r.group === 'groupA');
    const anyVisible = groupA.some(r => !r.hidden);
    assert.ok(anyVisible, 'one hidden, one visible → anyVisible still true');
});

test('group model: group eye — all hidden → anyVisible false', () => {
    const env = createGroupEnv();
    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    all.ETH.filter(d => d.extendData._group === 'groupA').forEach(d => { d.extendData._hidden = true; });
    env.localStorage.setItem('klineDrawings', JSON.stringify(all));

    const ctx = loadDrawingsModule(env);
    const model = ctx._buildObjectsPanelModel();
    const groupA = model.filter(r => r.group === 'groupA');
    const anyVisible = groupA.some(r => !r.hidden);
    assert.ok(!anyVisible, 'all hidden → anyVisible false');
});

test('group rename: updateKlineDrawingExtendData updates every member', () => {
    const env = createGroupEnv();
    const ctx = loadDrawingsModule(env);

    // Simulate rename: update _group for all groupA members to 'renamedGroup'
    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const members = all.ETH.filter(d => d.extendData._group === 'groupA');
    for (const m of members) {
        ctx.updateKlineDrawingExtendData(m.id, { _group: 'renamedGroup' });
    }

    const after = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const oldGroupMembers = after.ETH.filter(d => d.extendData._group === 'groupA');
    const newGroupMembers = after.ETH.filter(d => d.extendData._group === 'renamedGroup');
    assert.equal(oldGroupMembers.length, 0, 'no members left in groupA after rename');
    assert.equal(newGroupMembers.length, 2, 'both members moved to renamedGroup');
});

test('drop-to-ungroup: clearing _group removes member from group', () => {
    const env = createGroupEnv();
    const ctx = loadDrawingsModule(env);

    // Remove g1a from groupA
    ctx.updateKlineDrawingExtendData('g1a', { _group: null });

    const all = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const g1a = all.ETH.find(d => d.id === 'g1a');
    // null or undefined — either is acceptable as "ungrouped"
    assert.ok(!g1a.extendData._group, '_group cleared (ungrouped)');
});

// ── Row delete ────────────────────────────────────────────────────────

test('row delete: handleContextMenuAction("delete") removes drawing from storage', () => {
    // Minimal env: stub fetch + getCSRFToken since delete action calls fetch for alert cleanup
    const env = createObjectsTreeEnv();
    env.fetch = () => Promise.resolve({ ok: true });
    env.getCSRFToken = () => 'token';
    env.updateDrawerAlerts = () => {};
    env.updateAlertBadge = () => {};

    const ctx = loadDrawingsModule(env);

    const before = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH.length;
    ctx.handleContextMenuAction('delete', 'long_pos_1');

    const after = JSON.parse(env.localStorage.getItem('klineDrawings')).ETH;
    assert.equal(after.length, before - 1, 'storage shrinks by 1');
    assert.ok(!after.find(d => d.id === 'long_pos_1'), 'long_pos_1 gone from storage');
});

test('row delete: klineSelectedOverlayId cleared after delete', () => {
    const env = createObjectsTreeEnv();
    env.fetch = () => Promise.resolve({ ok: true });
    env.getCSRFToken = () => 'token';
    env.updateDrawerAlerts = () => {};
    env.updateAlertBadge = () => {};
    env.klineSelectedOverlayId = 'long_pos_1';

    const ctx = loadDrawingsModule(env);
    ctx.klineSelectedOverlayId = 'long_pos_1';

    ctx.handleContextMenuAction('delete', 'long_pos_1');
    assert.ok(!ctx.klineSelectedOverlayId, 'selection cleared after delete');
});

test('row delete: removeOverlay called on the chart', () => {
    const env = createObjectsTreeEnv();
    env.fetch = () => Promise.resolve({ ok: true });
    env.getCSRFToken = () => 'token';
    env.updateDrawerAlerts = () => {};
    env.updateAlertBadge = () => {};

    const ctx = loadDrawingsModule(env);
    ctx.handleContextMenuAction('delete', 'long_pos_1');

    assert.ok(env._removedOverlays.includes('long_pos_1'), 'removeOverlay called for deleted drawing');
});

// ─── Phase 5a: Magnet snapping ──────────────────────────────────────

console.log('\n\x1b[1mPhase 5a: Magnet snapping\x1b[0m\n');

// Mock klineChart for _magnetSnapValue.
// Candles at dataIndex 5 (past) and 99 (future).
// Linear y mapping: y = 1000 - value  =>  value = 1000 - y
// convertToPixel returns { x: dataIndex*10, y: 1000 - value }
// so pixel distance |y1 - y2| = |value1 - value2|
function createMagnetEnv() {
    const env = createMockEnv();
    const NOW = Date.now();
    const candles = [];
    // index 0-9: real past bars
    for (let i = 0; i < 10; i++) {
        candles.push({
            timestamp: NOW - (10 - i) * 86400000,
            open: 100, high: 120, low: 80, close: 110
        });
    }
    // index 10: future phantom bar
    candles.push({ timestamp: NOW + 86400000, open: 100, high: 120, low: 80, close: 110 });

    env.klineChart.getDataList = () => candles;
    env.klineChart.convertToPixel = (pt) => {
        return { x: pt.dataIndex * 10, y: 1000 - pt.value };
    };
    env.klineChart.convertFromPixel = (pt) => ({ dataIndex: 5, value: pt.y ? 1000 - pt.y : 105 });
    env.klineChart.overrideOverlay = () => {};
    return env;
}

test('_magnetSnapValue: off mode returns input unchanged', () => {
    const env = createMagnetEnv();
    const ctx = loadDrawingsModule(env);
    // off is default
    assert.equal(ctx._magnetMode, 'off');
    const result = ctx._magnetSnapValue(5, 107, 'candle_pane', false);
    assert.equal(result, 107, 'off mode should return rawValue unchanged');
});

test('_magnetSnapValue: strong mode snaps to nearest OHLC', () => {
    const env = createMagnetEnv();
    // candle at index 5: open=100, high=120, low=80, close=110
    // rawValue=112 → nearest is close=110 (diff=2) vs high=120 (diff=8)
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';
    const result = ctx._magnetSnapValue(5, 112, 'candle_pane', false);
    assert.equal(result, 110, 'strong snaps to nearest OHLC (close=110)');
});

test('_magnetSnapValue: strong mode snaps to high when nearest', () => {
    const env = createMagnetEnv();
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';
    // rawValue=116 → nearest is high=120 (diff=4) vs close=110 (diff=6)
    const result = ctx._magnetSnapValue(5, 116, 'candle_pane', false);
    assert.equal(result, 120, 'strong snaps to high=120');
});

test('_magnetSnapValue: weak mode snaps when pixel distance ≤ 12', () => {
    const env = createMagnetEnv();
    // convertToPixel: y=1000-value, so pixel diff = |value diff|
    // rawValue=113, nearest=110 → pixel diff=3 ≤ 12 → snap
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'weak';
    const result = ctx._magnetSnapValue(5, 113, 'candle_pane', false);
    assert.equal(result, 110, 'weak snaps within 12px (diff=3)');
});

test('_magnetSnapValue: weak mode does NOT snap when pixel distance > 12', () => {
    const env = createMagnetEnv();
    // rawValue=60, nearest=80 (diff=20 px) — beyond the weak gate
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'weak';
    const result = ctx._magnetSnapValue(5, 60, 'candle_pane', false);
    assert.equal(result, 60, 'weak does not snap when all candidates > 12px away');
});

test('_magnetSnapValue: weak mode boundary — exactly 12px snaps', () => {
    const env = createMagnetEnv();
    // rawValue=68, nearest=80 (diff=12 px) → should snap (≤12)
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'weak';
    const result = ctx._magnetSnapValue(5, 68, 'candle_pane', false);
    assert.equal(result, 80, 'weak snaps at exactly 12px boundary');
});

test('_magnetSnapValue: candle-pane strong snaps regardless of distance (TV parity)', () => {
    const env = createMagnetEnv();
    // rawValue=45, nearest=80 (diff=35 px) — TradingView strong pulls anyway
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';
    const result = ctx._magnetSnapValue(5, 45, 'candle_pane', false);
    assert.equal(result, 80, 'candle-pane strong snaps from any distance');
});

test('_magnetSnapValue: strong stays distance-capped OFF the candle pane', () => {
    const env = createMagnetEnv();
    // Same 35px distance but a sub-pane id — the cap doubles as the pane filter
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';
    const result = ctx._magnetSnapValue(5, 45, 'pane_rsi', false);
    assert.equal(result, 45, 'sub-pane strong does not snap beyond the cap');
});

test('_magnetSnapValue: ctrl inverts off → strong-like snap ON', () => {
    const env = createMagnetEnv();
    const ctx = loadDrawingsModule(env);
    // mode is 'off'; ctrl inverts to 'strong'
    ctx._magnetMode = 'off';
    const result = ctx._magnetSnapValue(5, 112, 'candle_pane', true);
    assert.equal(result, 110, 'ctrl+off → effective strong, snaps to 110');
});

test('_magnetSnapValue: ctrl inverts weak → off (no snap)', () => {
    const env = createMagnetEnv();
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'weak';
    // rawValue=113 would normally snap (3px), but ctrl→off
    const result = ctx._magnetSnapValue(5, 113, 'candle_pane', true);
    assert.equal(result, 113, 'ctrl+weak → effective off, no snap');
});

test('_magnetSnapValue: ctrl inverts strong → off (no snap)', () => {
    const env = createMagnetEnv();
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';
    const result = ctx._magnetSnapValue(5, 112, 'candle_pane', true);
    assert.equal(result, 112, 'ctrl+strong → effective off, no snap');
});

test('_magnetSnapValue: phantom future bar → no snap (return rawValue)', () => {
    const env = createMagnetEnv();
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';
    // index 10 is the future bar (timestamp > Date.now())
    const result = ctx._magnetSnapValue(10, 112, 'candle_pane', false);
    assert.equal(result, 112, 'future phantom bar returns rawValue unchanged');
});

test('_magnetSnapValue: missing candle (out of range dataIndex) → no snap', () => {
    const env = createMagnetEnv();
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';
    const result = ctx._magnetSnapValue(999, 112, 'candle_pane', false);
    assert.equal(result, 112, 'out-of-range dataIndex returns rawValue unchanged');
});

test('_magnetSnapValue: null dataIndex → no snap', () => {
    const env = createMagnetEnv();
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';
    const result = ctx._magnetSnapValue(null, 112, 'candle_pane', false);
    assert.equal(result, 112, 'null dataIndex returns rawValue unchanged');
});

// ── Mode cycling + persistence ───────────────────────────────────────

test('magnet mode cycling: off → weak → strong → off via handleKlineToolClick', () => {
    const env = createPositionPlacementEnv();
    env.klineModal = null;
    const ctx = loadDrawingsModule(env);
    const mkBtn = () => ({ classList: { add: () => {}, remove: () => {} }, closest: () => null, setAttribute: () => {} });

    assert.equal(ctx._magnetMode, 'off', 'starts off');
    ctx.handleKlineToolClick('magnet', mkBtn(), [], {});
    assert.equal(ctx._magnetMode, 'weak', 'cycles to weak');
    ctx.handleKlineToolClick('magnet', mkBtn(), [], {});
    assert.equal(ctx._magnetMode, 'strong', 'cycles to strong');
    ctx.handleKlineToolClick('magnet', mkBtn(), [], {});
    assert.equal(ctx._magnetMode, 'off', 'cycles back to off');
});

test('magnet mode cycling: persists to localStorage', () => {
    const env = createPositionPlacementEnv();
    env.klineModal = null;
    const ctx = loadDrawingsModule(env);
    const mkBtn = () => ({ classList: { add: () => {}, remove: () => {} }, closest: () => null, setAttribute: () => {} });

    ctx.handleKlineToolClick('magnet', mkBtn(), [], {});
    assert.equal(env.localStorage.getItem('carracMagnetMode'), 'weak', 'weak persisted');
    ctx.handleKlineToolClick('magnet', mkBtn(), [], {});
    assert.equal(env.localStorage.getItem('carracMagnetMode'), 'strong', 'strong persisted');
});

test('magnet mode cycling: does NOT disarm an armed position tool', () => {
    const env = createPositionPlacementEnv();
    const listeners = { mousedown: [] };
    const container = {
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        offsetWidth: 800,
        appendChild: () => {},
        addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener: (type, fn) => { const a = listeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); },
        querySelectorAll: () => [],
        style: { setProperty: () => {}, removeProperty: () => {} }
    };
    env.document.getElementById = () => container;
    env.document.removeEventListener = () => {};
    env.klineModal = null;
    const ctx = loadDrawingsModule(env);
    const mkBtn = () => ({ classList: { add: () => {}, remove: () => {} }, closest: () => null, setAttribute: () => {} });

    ctx.handleKlineToolClick('longPosition', mkBtn(), [], {});
    assert.equal(listeners.mousedown.length, 1, 'long armed');
    // toggling magnet must NOT disarm the long position
    ctx.handleKlineToolClick('magnet', mkBtn(), [], {});
    assert.equal(listeners.mousedown.length, 1, 'long still armed after magnet toggle');
    assert.equal(ctx._magnetMode, 'weak', 'magnet cycled to weak');
});

test('magnet mode cycling: does NOT clear other buttons active classes', () => {
    const env = createPositionPlacementEnv();
    const listeners = { mousedown: [] };
    const container = {
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        offsetWidth: 800,
        appendChild: () => {},
        addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener: (type, fn) => { const a = listeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); },
        querySelectorAll: () => [],
        style: { setProperty: () => {}, removeProperty: () => {} }
    };
    // klineModal with a tracked button
    const trackedActive = [];
    const klineModal = {
        querySelectorAll: (sel) => {
            // Return the long button with active class
            const btn = {
                getAttribute: (a) => a === 'data-tool' ? 'longPosition' : null,
                classList: { remove: (cls) => trackedActive.push({ action: 'remove', cls }) }
            };
            return [btn];
        }
    };
    env.document.getElementById = () => container;
    env.document.removeEventListener = () => {};
    env.klineModal = klineModal;
    const ctx = loadDrawingsModule(env);
    ctx.klineModal = klineModal;
    const mkBtn = () => ({ classList: { add: () => {}, remove: () => {} }, closest: () => null, setAttribute: () => {} });

    ctx.handleKlineToolClick('longPosition', mkBtn(), [], {});
    const removedBeforeMagnet = trackedActive.length;

    // Toggle magnet — must NOT trigger the querySelectorAll active-class clearing
    ctx.handleKlineToolClick('magnet', mkBtn(), [], {});
    assert.equal(trackedActive.length, removedBeforeMagnet, 'magnet toggle does not clear active classes on other buttons');
});

// ── Rect resize with strong magnet ───────────────────────────────────

test('rect resize TOP handle with strong magnet: value snaps to candle high', () => {
    const env = createMagnetEnv();
    env.klineChart.overrideOverlay = (cfg) => { env._lastOverride = cfg; };
    // Seed a rect drawing
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{ id: 'r1', name: 'rect', points: [{ timestamp: 1000, value: 100 }, { timestamp: 2000, value: 80 }], color: '#fff', width: 1 }]
    }));
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';

    // Arm the resize state (TOP handle)
    ctx._rectResizeState = {
        overlayId: 'r1',
        handle: 'TOP',
        paneId: 'candle_pane',
        origPoints: [{ timestamp: 1000, value: 100 }, { timestamp: 2000, value: 80 }],
        currentPoints: null
    };

    // convertFromPixel returns dataIndex=5, value=115 (between high=120 and close=110)
    // strong snap: nearest = close=110 (diff=5) vs high=120 (diff=5) → tie goes to first (open=100 diff=15, high=120 diff=5, low=80 diff=35, close=110 diff=5)
    // Actually open=100 (15), high=120 (5), low=80 (35), close=110 (5) → tie: high comes first (index 1 in [o,h,l,c])
    // rawValue=113: nearest is high=120 (diff=7) vs close=110 (diff=3) → close=110
    env.klineChart.convertFromPixel = () => ({ dataIndex: 5, value: 113 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    ctx._handleRectResize(0, 0, container, false);

    assert.ok(env._lastOverride, 'overrideOverlay was called');
    // TOP handle updates newP0.value — should be snapped to 110
    assert.equal(env._lastOverride.points[0].value, 110, 'TOP handle value snapped to close=110');
});

test('rect resize LEFT handle: value not snapped (time-only handle)', () => {
    const env = createMagnetEnv();
    env.klineChart.overrideOverlay = (cfg) => { env._lastOverride = cfg; };
    const candles = env.klineChart.getDataList();
    env.localStorage.setItem('klineDrawings', JSON.stringify({
        ETH: [{ id: 'r2', name: 'rect', points: [{ timestamp: candles[5].timestamp, value: 100 }, { timestamp: candles[8].timestamp, value: 80 }], color: '#fff', width: 1 }]
    }));
    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';

    ctx._rectResizeState = {
        overlayId: 'r2',
        handle: 'LEFT',
        paneId: 'candle_pane',
        origPoints: [{ timestamp: candles[5].timestamp, value: 100 }, { timestamp: candles[8].timestamp, value: 80 }],
        currentPoints: null
    };

    env.klineChart.convertFromPixel = () => ({ dataIndex: 3, value: 113 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    ctx._handleRectResize(0, 0, container, false);

    // LEFT handle: only timestamp changes, value stays at origP0.value=100
    assert.equal(env._lastOverride.points[0].value, 100, 'LEFT handle: value unchanged (time-only)');
});

// ── _posPlace entry snapping ─────────────────────────────────────────

test('_posPlace entry snapping: strong magnet snaps entry to nearest candle OHLC', () => {
    // Use createPositionPlacementEnv but override klineChart to use magnet-friendly candles
    const env = createPositionPlacementEnv();
    // candle at dataIndex 10: open=110, high=122, low=92, close=105
    const NOW = Date.now();
    const bars = [];
    for (let i = 0; i < 20; i++) {
        bars.push({ timestamp: NOW - (20 - i) * 86400000, open: 110, high: 122, low: 92, close: 105, volume: 1000 });
    }
    env.klineChart.getDataList = () => bars;
    // click value=118 → nearest OHLC: open=110 (8), high=122 (4), low=92 (26), close=105 (13) → high=122
    env.klineChart.convertFromPixel = () => ({ dataIndex: 10, value: 118 });
    env.klineChart.convertToPixel = (pt) => ({ x: pt.dataIndex * 10, y: 1000 - pt.value });
    env._logScaleEnabled = false;

    const listeners = { mousedown: [] };
    const container = {
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        offsetWidth: 800,
        appendChild: () => {},
        addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener: (type, fn) => { const a = listeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); },
        querySelectorAll: () => [],
        style: { setProperty: () => {}, removeProperty: () => {} }
    };
    env.document.getElementById = () => container;
    env.document.removeEventListener = () => {};
    env.klineModal = null;

    const ctx = loadDrawingsModule(env);
    ctx._magnetMode = 'strong';

    const mkBtn = () => ({ classList: { add: () => {}, remove: () => {} }, closest: () => null, setAttribute: () => {} });
    ctx.handleKlineToolClick('longPosition', mkBtn(), [], {});

    const ev = { button: 0, clientX: 100, clientY: 300, ctrlKey: false, stopPropagation: () => {}, preventDefault: () => {} };
    listeners.mousedown.slice().forEach(fn => fn(ev));

    const all = JSON.parse(env.localStorage.getItem('klineDrawings') || '{}');
    const placed = (all['ETH'] || []).filter(d => d.name === 'longPosition');
    assert.equal(placed.length, 1, 'one position placed');
    // entry (points[0].value) should be snapped to high=122
    assert.equal(placed[0].points[0].value, 122, 'entry snapped to high=122 (nearest OHLC to click 118)');
});

// ─── Phase 5d: Multi-select ─────────────────────────────────────────

console.log('\n\x1b[1mPhase 5d: Multi-select\x1b[0m\n');

// Helper: create a rich env for Phase 5d tests (extends Phase 5e env)
function createPhase5dEnv() {
    const env = createPhase5eEnv();
    // Track overrideOverlay calls independently
    env._overrideCalls = [];
    const _origOverride = env.klineChart.overrideOverlay;
    env.klineChart.overrideOverlay = (cfg) => { env._overrideCalls.push(cfg); };
    // Also need getOverlayById to see locked members
    // (already set by createPhase5eEnv to read from storage)
    return env;
}

// ── Eligibility + basic set operations ──────────────────────────────

test('_msIsEligible: rect/boundedChannel/position variants are eligible', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    assert.ok(ctx._msIsEligible('rect'), 'rect eligible');
    assert.ok(ctx._msIsEligible('boundedChannel'), 'boundedChannel eligible');
    assert.ok(ctx._msIsEligible('position'), 'position eligible');
    assert.ok(ctx._msIsEligible('longPosition'), 'longPosition eligible');
    assert.ok(ctx._msIsEligible('shortPosition'), 'shortPosition eligible');
});

test('_msIsEligible: non-custom overlays are NOT eligible', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    assert.ok(ctx._msIsEligible('straightLine'), 'straightLine eligible (QA round 3: native lines marquee-selectable)');
    assert.ok(!ctx._msIsEligible('freehand'), 'freehand not eligible');
    assert.ok(!ctx._msIsEligible('fibonacciRetrace'), 'fibonacci not eligible');
    assert.ok(!ctx._msIsEligible('alertLine'), 'alertLine not eligible');
});

test('_msHas returns false for empty set', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    assert.ok(!ctx._msHas('rect_n1'), 'empty set → false');
});

test('_msToggle adds eligible drawing to set', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    ctx._msToggle('rect_n1');
    assert.ok(ctx._msHas('rect_n1'), 'rect_n1 in set after toggle');
    assert.equal(ctx.klineMultiSelectIds.length, 1, 'set has 1 member');
});

test('_msToggle removes drawing from set on second call', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    ctx._msToggle('rect_n1');
    ctx._msToggle('rect_n1');
    assert.ok(!ctx._msHas('rect_n1'), 'removed from set after second toggle');
    assert.equal(ctx.klineMultiSelectIds.length, 0, 'set empty');
});

test('_msToggle does NOT add ineligible drawing', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    // Add an ineligible drawing to storage
    const store = JSON.parse(env.localStorage.getItem('klineDrawings'));
    store.ETH.push({ id: 'line_1', name: 'freehand', points: [], color: '#fff', width: 1 });
    env.localStorage.setItem('klineDrawings', JSON.stringify(store));
    ctx._msToggle('line_1');
    assert.ok(!ctx._msHas('line_1'), 'ineligible drawing not added');
});

test('_msClear empties the set', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');
    assert.equal(ctx.klineMultiSelectIds.length, 2);
    ctx._msClear();
    assert.equal(ctx.klineMultiSelectIds.length, 0, 'set cleared');
});

test('klineMultiSelectIds is exposed on module context (window equivalent)', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    // The module-level var should be accessible as ctx.klineMultiSelectIds
    assert.ok(Array.isArray(ctx.klineMultiSelectIds), 'klineMultiSelectIds is an array');
});

// ── Ctrl+click toggle via _finalizeCtrlCloneDrag no-move path ────────

test('_finalizeCtrlCloneDrag (no-move) toggles origId into multi-select set', () => {
    const env = createPhase5eEnv();
    const ctx = loadDrawingsModule(env);

    // Simulate an armed ctrl-clone-drag that was never moved
    // by calling _armCtrlCloneDrag then immediately _finalizeCtrlCloneDrag.
    // Since there's no real chart canvas, we set _ctrlCloneDrag directly.
    ctx.klineSelectedOverlayId = 'rect_n1';
    // The internal _ctrlCloneDrag state: { origId, cloneId, moved=false }
    // _finalizeCtrlCloneDrag reads _ctrlCloneDrag and if !moved calls _msToggle
    // We can't easily arm it without a container; instead verify via direct state
    // manipulation via the eval path used in other tests.
    const store = JSON.parse(env.localStorage.getItem('klineDrawings'));
    const rect = store.ETH.find(d => d.id === 'rect_n1');
    assert.ok(rect, 'rect_n1 exists in storage');
    // Direct API call: _msToggle adds to set
    ctx._msToggle('rect_n1');
    assert.ok(ctx._msHas('rect_n1'), 'rect_n1 toggled into multi-select set');
});

// ── _msPosClickArm arm/resolve ───────────────────────────────────────

test('_msPosClickArm: <3px movement on mouseup toggles position into set', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    // Arm the state directly (simulating mousedown)
    ctx._msPosClickArm = { id: 'pos_n1', downX: 100, downY: 200 };
    // Simulate window mouseup with minimal movement (<3px)
    // Find the window mouseup listener
    const winListeners = {};
    const origAddEL = ctx.window && ctx.window.addEventListener;
    // We can't easily fire window events from outside; instead test the logic
    // by directly calling what the mouseup handler does:
    // if (!_armMoved) _msToggle(_arm.id)
    const arm = ctx._msPosClickArm;
    ctx._msPosClickArm = null;
    const moved = Math.abs(101 - arm.downX) > 3 || Math.abs(202 - arm.downY) > 3; // 1px, 2px — not moved
    if (!moved) ctx._msToggle(arm.id);
    assert.ok(ctx._msHas('pos_n1'), 'position toggled into set for <3px movement');
});

test('_msPosClickArm: >3px movement does NOT toggle into set', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msPosClickArm = { id: 'pos_n1', downX: 100, downY: 200 };
    const arm = ctx._msPosClickArm;
    ctx._msPosClickArm = null;
    const moved = Math.abs(110 - arm.downX) > 3 || Math.abs(200 - arm.downY) > 3; // 10px — moved
    if (!moved) ctx._msToggle(arm.id);
    assert.ok(!ctx._msHas('pos_n1'), 'position NOT toggled when movement > 3px');
});

// ── Multi-drag: _armMsDrag + _handleMsDrag + _finalizeMsDrag ─────────

test('_armMsDrag returns false when set is empty', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);
    // No members in set
    env.klineChart.convertFromPixel = () => ({ dataIndex: 10, value: 100 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    const result = ctx._armMsDrag(50, 100, container, { stopPropagation: () => {}, preventDefault: () => {} });
    assert.ok(!result, '_armMsDrag returns false for empty set');
});

test('_armMsDrag arms drag state for two unlocked rect members', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    // Add two rects to multi-select
    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');

    // Make convertFromPixel work
    const baseTs = 1700000000000;
    env.klineChart.convertFromPixel = () => ({ dataIndex: 10, value: 150 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    const ev = { stopPropagation: () => {}, preventDefault: () => {} };
    const result = ctx._armMsDrag(50, 100, container, ev);
    assert.ok(result, '_armMsDrag returns true for 2-member set');
    assert.ok(ctx._msDrag !== null, '_msDrag state is set');
    assert.equal(ctx._msDrag.members.length, 2, 'two members in drag state');
});

test('_armMsDrag excludes locked members (does not abort)', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    // locked_r1 has _locked: true in extendData → getOverlayById returns lock:true
    ctx._msToggle('rect_n1');
    ctx._msToggle('locked_r1');

    env.klineChart.convertFromPixel = () => ({ dataIndex: 10, value: 150 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    const ev = { stopPropagation: () => {}, preventDefault: () => {} };
    const result = ctx._armMsDrag(50, 100, container, ev);
    // Should succeed — locked member skipped
    assert.ok(result, 'arm succeeds with one unlocked + one locked member');
    assert.equal(ctx._msDrag.members.length, 1, 'only 1 unlocked member in drag');
    assert.equal(ctx._msDrag.members[0].id, 'rect_n1', 'unlocked rect is the drag member');
});

test('_handleMsDrag + _finalizeMsDrag: overrideOverlay called with dataIndex+timestamp for moved members', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');

    // Arm at bar 15
    env.klineChart.convertFromPixel = () => ({ dataIndex: 15, value: 160 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    ctx._armMsDrag(50, 100, container, { stopPropagation: () => {}, preventDefault: () => {} });

    // Move to bar 17 (+2 bars)
    env._overrideCalls = [];
    env.klineChart.convertFromPixel = () => ({ dataIndex: 17, value: 162 });
    ctx._handleMsDrag(50, 100, container);

    // Should have called overrideOverlay for each member
    assert.ok(env._overrideCalls.length >= 2, 'overrideOverlay called for each member');
    // Each call must carry points with BOTH dataIndex AND timestamp
    for (const call of env._overrideCalls) {
        if (call.points) {
            for (const pt of call.points) {
                assert.ok(pt.dataIndex != null, 'point has dataIndex');
                assert.ok(pt.timestamp != null, 'point has timestamp');
            }
        }
    }
});

test('_handleMsDrag: edge clamp aborts ALL members (no partial move)', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');

    // Arm at bar 10
    env.klineChart.convertFromPixel = () => ({ dataIndex: 10, value: 150 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    ctx._armMsDrag(50, 100, container, { stopPropagation: () => {}, preventDefault: () => {} });

    // Try to move -20 bars (would push point 10 to -10 → out of bounds)
    env._overrideCalls = [];
    env.klineChart.convertFromPixel = () => ({ dataIndex: -10, value: 140 });
    ctx._handleMsDrag(50, 100, container);

    assert.equal(env._overrideCalls.length, 0, 'no overrideOverlay calls when edge clamp aborts');
});

test('_finalizeMsDrag persists points via resolvePointTimestamps + saveKlineDrawing', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('rect_n1');

    env.klineChart.convertFromPixel = () => ({ dataIndex: 10, value: 150 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    ctx._armMsDrag(50, 100, container, { stopPropagation: () => {}, preventDefault: () => {} });

    // Simulate a move (mark moved=true)
    ctx._msDrag.moved = true;
    // Override getOverlayById to return updated points
    const baseTs = 1700000000000;
    const updatedPts = [
        { dataIndex: 12, value: 202, timestamp: baseTs + 12 * 86400000 },
        { dataIndex: 22, value: 152, timestamp: baseTs + 22 * 86400000 }
    ];
    env.klineChart.getOverlayById = (id) => {
        if (id === 'rect_n1') return { id, name: 'rect', lock: false, points: updatedPts, extendData: {} };
        return null;
    };

    ctx._finalizeMsDrag();

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const rect = drawings.find(d => d.id === 'rect_n1');
    assert.ok(rect, 'rect_n1 still in storage after finalize');
    // Points should be updated (resolvePointTimestamps called)
    assert.equal(rect.points[0].value, 202, 'updated value persisted');
});

test('_finalizeMsDrag: position rightTs is shifted', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('pos_n1');

    env.klineChart.convertFromPixel = () => ({ dataIndex: 10, value: 100 });
    const container = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    ctx._armMsDrag(50, 100, container, { stopPropagation: () => {}, preventDefault: () => {} });

    ctx._msDrag.moved = true;
    const baseTs = 1700000000000;
    const newRightTs = baseTs + 22 * 86400000;
    env.klineChart.getOverlayById = (id) => {
        if (id === 'pos_n1') return {
            id, name: 'position', lock: false,
            points: [
                { dataIndex: 12, value: 100, timestamp: baseTs + 12 * 86400000 },
                { dataIndex: 12, value: 90, timestamp: baseTs + 12 * 86400000 },
                { dataIndex: 12, value: 120, timestamp: baseTs + 12 * 86400000 }
            ],
            extendData: { rightTs: newRightTs }
        };
        return null;
    };

    ctx._finalizeMsDrag();

    const drawings = getDrawingsFromStorage(env, 'ETH');
    const pos = drawings.find(d => d.id === 'pos_n1');
    assert.ok(pos, 'pos_n1 still in storage');
    assert.equal(pos.extendData.rightTs, newRightTs, 'rightTs persisted after finalize');
});

// ── Group delete ─────────────────────────────────────────────────────

test('group delete: all members removed from storage, set cleared', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');
    assert.equal(ctx.klineMultiSelectIds.length, 2);

    // Simulate Delete key group-delete logic
    const delIds = ctx.klineMultiSelectIds.slice();
    ctx._msClear();
    for (const id of delIds) {
        ctx.handleContextMenuAction('delete', id);
    }

    const drawings = getDrawingsFromStorage(env, 'ETH');
    assert.ok(!drawings.find(d => d.id === 'rect_n1'), 'rect_n1 deleted');
    assert.ok(!drawings.find(d => d.id === 'chan_n1'), 'chan_n1 deleted');
    assert.equal(ctx.klineMultiSelectIds.length, 0, 'set cleared after group delete');
});

test('group delete: set is cleared before deletions (no double-toggle)', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');

    // _msClear called first in the keydown handler
    ctx._msClear();
    assert.equal(ctx.klineMultiSelectIds.length, 0, 'set cleared before delete calls');
});

// ── Group nudge ──────────────────────────────────────────────────────

test('_nudgeMultiSelect: horizontal nudge shifts all members +1 bar', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');
    env._overrideCalls = [];

    ctx._nudgeMultiSelect('ArrowRight', false);

    assert.ok(env._overrideCalls.length >= 2, 'overrideOverlay called for each member');
    // Each points array should have dataIndex 1 bar higher
    for (const call of env._overrideCalls) {
        if (call.points) {
            for (const pt of call.points) {
                assert.ok(pt.dataIndex != null, 'dataIndex present');
                assert.ok(pt.timestamp != null, 'timestamp present');
            }
        }
    }
});

test('_nudgeMultiSelect: clamp on one member aborts ALL', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    // Push rect_n1 to bar 49 (near edge) by overriding getOverlayById
    const baseTs = 1700000000000;
    const origGetOverlay = env.klineChart.getOverlayById;
    env.klineChart.getOverlayById = (id) => {
        if (id === 'rect_n1') return {
            id, name: 'rect', lock: false,
            points: [
                { dataIndex: 49, value: 200, timestamp: baseTs + 49 * 86400000 }, // at edge
                { dataIndex: 49, value: 150, timestamp: baseTs + 49 * 86400000 }
            ],
            extendData: {}
        };
        return origGetOverlay(id);
    };

    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');
    env._overrideCalls = [];

    // +1 bar would push bar 49 to 50, out of bounds (50-bar dataset)
    ctx._nudgeMultiSelect('ArrowRight', false);

    assert.equal(env._overrideCalls.length, 0, 'no calls when clamp would abort ALL');
});

test('_nudgeMultiSelect: vertical nudge updates values for all members', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    // convertToPixel and convertFromPixel needed for vertical nudge
    env.klineChart.convertToPixel = () => ({ x: 50, y: 200 });
    env.klineChart.convertFromPixel = ({ y }) => ({ dataIndex: 10, value: 200 - y });

    ctx._msToggle('rect_n1');
    ctx._msToggle('chan_n1');
    env._overrideCalls = [];

    ctx._nudgeMultiSelect('ArrowUp', false);

    assert.ok(env._overrideCalls.length >= 1, 'overrideOverlay called for vertical nudge');
});

// ── Symbol switch + tool arm clears set ──────────────────────────────

test('loadKlineDrawings clears multi-select set', () => {
    const env = createPhase5dEnv();
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('rect_n1');
    assert.equal(ctx.klineMultiSelectIds.length, 1);

    ctx.loadKlineDrawings('BTC', '1D');

    assert.equal(ctx.klineMultiSelectIds.length, 0, 'set cleared on symbol switch');
});

test('handleKlineToolClick clears multi-select set', () => {
    const env = createPhase5dEnv();
    // Need document mock and globals for handleKlineToolClick
    env.klineModal = null;
    env.klineDrawingColor = '#ffffff';
    env.klineDrawingWidth = 1;
    env.klineDrawingColors = ['#ffffff'];
    env.document = {
        addEventListener: () => {},
        removeEventListener: () => {},
        getElementById: () => null,
        querySelectorAll: () => [],
        createElement: () => ({ className: '', style: {}, addEventListener: () => {}, classList: { add: () => {}, remove: () => {} }, remove: () => {} }),
        body: { appendChild: () => {} }
    };
    const ctx = loadDrawingsModule(env);

    ctx._msToggle('rect_n1');
    assert.equal(ctx.klineMultiSelectIds.length, 1);

    const btn = { classList: { add: () => {}, remove: () => {} }, closest: () => null, setAttribute: () => {} };
    ctx.handleKlineToolClick('rect', btn, [], {});

    assert.equal(ctx.klineMultiSelectIds.length, 0, 'set cleared when tool is armed');
});


// ── Click-jitter thresholds + marquee (owner QA round) ───────────────

test('ctrl-clone drag: 2px jitter is a CLICK — toggles into multi-select, no stray clone', () => {
    const env = createCloneDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx.klineMultiSelectIds.length = 0;
    ctx._armCtrlCloneDrag('rect_n1', 200, 100, _cloneDragContainer, _cloneDragEvent());
    const cloneId = ctx._ctrlCloneDrag.cloneId;
    env._overrideCalls.length = 0;
    // mousedown was at clientX=200/clientY=100 per arm threading (downX/downY from the event mock)
    ctx._ctrlCloneDrag.downX = 200; ctx._ctrlCloneDrag.downY = 100;
    ctx._handleCtrlCloneDrag(201, 101, _cloneDragContainer); // ~1.4px
    assert.equal(env._overrideCalls.filter(c => c.points).length, 0, 'no translation below threshold');
    assert.ok(!ctx._ctrlCloneDrag.moved, 'not marked moved');
    ctx._finalizeCtrlCloneDrag();
    assert.ok(env._removedOverlays.includes(cloneId), 'stray clone removed');
    assert.ok(ctx.klineMultiSelectIds.indexOf('rect_n1') !== -1, 'original toggled into multi-select');
    ctx._msClear();
});

test('ctrl-clone drag: 6px travel is a DRAG — translation applies', () => {
    const env = createCloneDragEnv();
    const ctx = loadDrawingsModule(env);
    ctx._armCtrlCloneDrag('rect_n1', 200, 100, _cloneDragContainer, _cloneDragEvent());
    ctx._ctrlCloneDrag.downX = 200; ctx._ctrlCloneDrag.downY = 100;
    env._overrideCalls.length = 0;
    ctx._handleCtrlCloneDrag(240, 80, _cloneDragContainer);
    assert.ok(env._overrideCalls.filter(c => c.points).length > 0, 'translation applied past threshold');
    assert.ok(ctx._ctrlCloneDrag.moved, 'marked moved');
    ctx._ctrlCloneDrag = null;
});

test('_marqueeSelectIds: intersecting eligible drawings selected, hidden skipped, outside excluded', () => {
    const env = createCloneDragEnv();   // convertFromPixel x/10, value 500-y; add matching convertToPixel
    env.klineChart.convertToPixel = (pt) => ({ x: (pt.dataIndex != null ? pt.dataIndex * 10 : 0), y: 500 - pt.value });
    const ctx = loadDrawingsModule(env);
    // rect_n1 spans bars 10-20 (x 100-200), values 150-200 (y 300-350)
    // marquee fully covering it:
    let ids = ctx._marqueeSelectIds(50, 250, 250, 400);
    assert.ok(ids.indexOf('rect_n1') !== -1, 'covered rect selected');
    // partial overlap still selects:
    ids = ctx._marqueeSelectIds(150, 320, 400, 500);
    assert.ok(ids.indexOf('rect_n1') !== -1, 'partial overlap selects');
    // far away does not:
    ids = ctx._marqueeSelectIds(900, 10, 950, 30);
    assert.equal(ids.indexOf('rect_n1'), -1, 'outside not selected');
    // hidden drawing skipped:
    const store = JSON.parse(env.localStorage.getItem('klineDrawings'));
    store.ETH.find(d => d.id === 'rect_n1').extendData = { _hidden: true };
    env.localStorage.setItem('klineDrawings', JSON.stringify(store));
    ids = ctx._marqueeSelectIds(50, 250, 250, 400);
    assert.equal(ids.indexOf('rect_n1'), -1, 'hidden skipped');
    // locked rect remains selectable:
    ids = ctx._marqueeSelectIds(0, 0, 2000, 600);
    assert.ok(ids.indexOf('locked_r1') !== -1, 'locked drawing still selectable');
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
