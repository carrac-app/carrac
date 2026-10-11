// Source-level guards for two Bugsink-reported browser crashes (2026-09-05).
//
// 1. requestNotificationPermission() (app-core.js) referenced the bare `Notification`
//    global. iOS Safari (outside an installed PWA) has no such global, so the
//    reference itself throws ReferenceError inside window.onload and the whole
//    post-load chain (loadData, WebSocket, split layout) never runs.
// 2. scenarios.js checks `klineChart && klineChart.resize` BEFORE a 220ms
//    setTimeout, then calls klineChart.resize() inside the callback without
//    re-checking — the chart can be torn down in that window.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const { chartAppSource } = require('./chart-app-sources.cjs');

const APP = chartAppSource();
const SCENARIOS = fs.readFileSync(path.join(ROOT, 'static/js/scenarios.js'), 'utf8');

function extractFunction(src, name) {
    const start = src.indexOf(`function ${name}(`);
    assert.notStrictEqual(start, -1, `${name} not found`);
    let depth = 0, i = src.indexOf('{', start);
    for (; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}' && --depth === 0) break;
    }
    return src.slice(start, i + 1);
}

test('requestNotificationPermission survives a window with no Notification global', () => {
    const fn = extractFunction(APP, 'requestNotificationPermission');
    const ctx = vm.createContext({});            // no Notification here, like iOS Safari
    vm.runInContext(fn + '\nrequestNotificationPermission();', ctx);
});

test('requestNotificationPermission still asks when Notification exists and is default', () => {
    const fn = extractFunction(APP, 'requestNotificationPermission');
    let asked = 0;
    const ctx = vm.createContext({ Notification: { permission: 'default', requestPermission: () => { asked++; } } });
    vm.runInContext(fn + '\nrequestNotificationPermission();', ctx);
    assert.strictEqual(asked, 1);
});

test('scenarios.js re-checks klineChart inside every deferred resize', () => {
    const unguarded = SCENARIOS.match(/setTimeout\(function\s*\(\)\s*\{\s*klineChart\.resize\(\);\s*\}/g) || [];
    assert.deepStrictEqual(unguarded, [], `unguarded deferred resize calls: ${unguarded.length}`);
});
