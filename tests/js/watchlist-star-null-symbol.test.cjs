/**
 * updateStarIcons() must survive a .star element that carries no
 * data-symbol, and the details drawer must stop rendering one.
 *
 * A star without data-symbol makes getAttribute('data-symbol') return null,
 * and isInWatchlist(null) would throw on null.toUpperCase(), escaping the
 * NodeList.forEach and aborting the rest of renderFilteredTable/renderData/
 * loadData. The drawer header star (details-drawer.js, #drStar) outlives the
 * drawer, so it is in the DOM for every later updateStarIcons().
 *
 * Two independent guards here on purpose: one pins the caller (isInWatchlist
 * tolerates a missing symbol), one pins the cause (the drawer emits the
 * attribute like every other star).
 *
 * Slices the two functions out of their files with the same VM pattern as
 * measure-tool-label.test.cjs.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createContext, Script } = require('node:vm');
const path = require('node:path');

const read = (f) => readFileSync(path.join(__dirname, '../../static/js/', f), 'utf8');

function slice(src, startMarker, endMarker) {
    const start = src.indexOf(startMarker);
    assert.ok(start >= 0, `marker not found: ${startMarker}`);
    const end = src.indexOf(endMarker, start);
    assert.ok(end > start, `end marker not found: ${endMarker}`);
    return src.slice(start, end);
}

/** A .star node: `symbol` of null models an element with no data-symbol attr. */
function fakeStar(symbol) {
    return {
        _symbol: symbol,
        innerHTML: '',
        classes: new Set(),
        getAttribute(name) {
            return name === 'data-symbol' ? this._symbol : null;
        },
        classList: {
            add(c) { this._o.classes.add(c); },
            remove(c) { this._o.classes.delete(c); },
        },
    };
}

function withStars(stars) {
    stars.forEach((s) => { s.classList._o = s; });
    const selectors = [];
    const env = {
        watchlistSymbols: ['BTC', 'ETH'],
        // Returns the stars regardless of selector on purpose: the guard inside
        // isInWatchlist must hold even if a caller hands it an unfiltered list.
        document: {
            querySelectorAll: (sel) => { selectors.push(sel); return stars; },
        },
        _selectors: selectors,
        console, Object, Array,
    };
    env.window = env;
    const code =
        slice(read('watchlist.js'), 'function isInWatchlist(', 'function getAllWatchlistSymbols(') +
        slice(read('screener-table.js'), 'function updateStarIcons()', '// Watchlist rows are now rendered') // moved out of main.js;
    new Script(code, { filename: 'star-slice.js' }).runInContext(createContext(env));
    return env;
}

test('a .star with no data-symbol does not throw', () => {
    const stars = [fakeStar('BTC'), fakeStar(null), fakeStar('SOL')];
    const env = withStars(stars);

    assert.doesNotThrow(() => env.updateStarIcons());
});

test('stars after the attribute-less one are still updated', () => {
    // The real damage was not the throw itself but everything it skipped.
    const stars = [fakeStar(null), fakeStar('BTC'), fakeStar('SOL')];
    const env = withStars(stars);

    env.updateStarIcons();

    assert.ok(stars[1].classes.has('active'), 'BTC is in the watchlist and must be starred');
    assert.ok(!stars[2].classes.has('active'), 'SOL is not in the watchlist');
});

test('isInWatchlist tolerates a missing symbol instead of throwing', () => {
    const env = withStars([]);

    assert.equal(env.isInWatchlist(null), false);
    assert.equal(env.isInWatchlist(undefined), false);
    assert.equal(env.isInWatchlist(''), false);
    assert.equal(env.isInWatchlist('btc'), true, 'case-insensitive match must still work');
});

test('updateStarIcons only claims stars that name a symbol', () => {
    // The cause, not the symptom. The drawer star is self-managed (its own
    // click handler toggles `.on`, styled by `#cd-drawer .dh-top .star.on`),
    // so this loop must not reach for it at all.
    const env = withStars([]);

    env.updateStarIcons();

    assert.equal(env._selectors.length, 1);
    assert.match(env._selectors[0], /\[data-symbol\]/,
        'selector must exclude stars with no data-symbol');
});

test('the drawer star is still the one .star rendered without a data-symbol', () => {
    // If someone later gives #drStar a data-symbol, updateStarIcons would start
    // setting `.active` on a button styled by `.on` and overwrite its glyph.
    // That is a deliberate design choice, not a detail to change silently.
    const drawer = read('details-drawer.js');
    const star = drawer.split('\n').find((l) => l.includes('id="drStar"'));
    assert.ok(star, 'drawer star markup not found — did the id change?');
    assert.doesNotMatch(star, /data-symbol=/,
        'drawer star gained a data-symbol — keeping it needs a null-symbol guard');
});
