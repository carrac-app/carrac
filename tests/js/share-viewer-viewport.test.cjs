'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { decideShareViewport } = require('../../static/js/share-chart-viewer.js');

const DAY = 86400;
const times = Array.from({ length: 100 }, (_, i) => 1700000000 + i * DAY);

test('no viewport -> null (viewer keeps klinecharts default fit)', () => {
  assert.strictEqual(decideShareViewport(times, null, 800), null);
  assert.strictEqual(decideShareViewport(times, {}, 800), null);
});

test('viewport with <2 covered bars -> null', () => {
  assert.strictEqual(
    decideShareViewport(times, { from_ts: 1, to_ts: 2 }, 800), null);
});

test('bar space spreads chart width over the visible bars', () => {
  const vp = { from_ts: times[20], to_ts: times[59] };  // 40 bars
  const d = decideShareViewport(times, vp, 800);
  assert.strictEqual(d.scrollTs, times[59] * 1000);     // ms for klinecharts
  assert.strictEqual(d.barSpace, 20);                    // 800 / 40
});

test('bar space clamps to sane bounds [2, 26]', () => {
  const wide = decideShareViewport(
    times, { from_ts: times[0], to_ts: times[99] }, 120);
  assert.strictEqual(wide.barSpace, 2);                  // 120/100 clamped up
  const narrow = decideShareViewport(
    times, { from_ts: times[0], to_ts: times[3] }, 800);
  assert.strictEqual(narrow.barSpace, 26);               // 200 clamped down
});
