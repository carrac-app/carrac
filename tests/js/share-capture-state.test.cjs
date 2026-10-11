'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { computeVisibleDrawingIds } = require('../../static/js/share-modal.js');

test('null/garbage input -> []', () => {
  assert.deepStrictEqual(computeVisibleDrawingIds(null, false), []);
  assert.deepStrictEqual(computeVisibleDrawingIds('x', false), []);
});

test('master hide-all wins (WYSIWYG)', () => {
  assert.deepStrictEqual(
    computeVisibleDrawingIds([{ id: 1 }, { id: 2 }], true), []);
});

test('individually hidden drawings are excluded', () => {
  assert.deepStrictEqual(
    computeVisibleDrawingIds([
      { id: 11 },
      { id: 12, extendData: { _hidden: true } },
      { id: 13, extendData: {} }
    ], false),
    [11, 13]);
});

test('non-numeric local ids are dropped', () => {
  assert.deepStrictEqual(
    computeVisibleDrawingIds([{ id: 'local_abc' }, { id: '42' }, {}], false),
    [42]);
});

test('caps at 100 ids (server model max)', () => {
  const many = Array.from({ length: 130 }, (_, i) => ({ id: i + 1 }));
  assert.strictEqual(computeVisibleDrawingIds(many, false).length, 100);
});
