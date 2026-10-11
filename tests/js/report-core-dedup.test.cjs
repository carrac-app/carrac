// tests/js/report-core-dedup.test.cjs
// Task 3: ReportCore.isSummaryRedundant — hide the Executive Summary card when it
// is just a truncated/flattened copy of the Overview (TL;DR). Run:
//   node --test tests/js/report-core-dedup.test.cjs
const { test } = require('node:test');
const assert = require('node:assert');
const ReportCore = require('../../static/js/report-core.js');

test('truncated TL;DR summary is detected as redundant', function() {
    const overview = 'Silver down 7.8% on 16x volume. DXY rising. BTC fear and greed at 20.';
    const summary = 'Silver down 7.8% on 16x volume. DXY rising. BTC fear and gr…'; // truncated prefix
    assert.strictEqual(ReportCore.isSummaryRedundant(summary, overview), true);
});

test('a genuinely different summary is kept', function() {
    const overview = 'Silver down 7.8% on 16x volume. DXY rising.';
    const summary = 'Operator note: holding XLP long into the PCE print.';
    assert.strictEqual(ReportCore.isSummaryRedundant(summary, overview), false);
});

test('empty / tiny inputs do not crash and are not redundant', function() {
    assert.strictEqual(ReportCore.isSummaryRedundant('', 'anything here at all'), false);
    assert.strictEqual(ReportCore.isSummaryRedundant('two words', ''), false);
});
