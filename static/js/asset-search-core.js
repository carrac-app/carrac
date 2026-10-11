/* Asset-search shared core — the bits that were duplicated verbatim
 * between asset-search.js (chart popup) and main-page-search.js (main page
 * Space-key overlay). Each caller now builds its own UI but delegates
 * ranking + server-fallback logic to this module.
 *
 * Public API (all on window for the pre-ESM bundle):
 *   assetSearchCore.filterAndRank(query, {includeFavorites})
 *       → { favs: [sym], others: [sym], all: [sym] }
 *   assetSearchCore.fetchDynamicSymbols(query, {seen, signal})
 *       → Promise<[{symbol, name, exchange, tier}]>
 *   assetSearchCore.matchRank(symbol, query) → int (0=exact, -1=no match)
 */
(function(global) {
    'use strict';

    function _getAllAssets() {
        return (global.allAssets && typeof global.allAssets === 'object') ? global.allAssets : {};
    }

    function _watchlistSet() {
        var set = {};
        if (typeof global.getAllWatchlistSymbols === 'function') {
            global.getAllWatchlistSymbols().forEach(function(s) { set[s] = true; });
        }
        return set;
    }

    function matchRank(sym, query) {
        if (!query) return 0;
        var sl = String(sym).toLowerCase();
        var meta = _getAllAssets()[sym] || {};
        var name = String(meta.name || '').toLowerCase();
        if (sl === query) return 0;
        if (sl.indexOf(query) === 0) return 1;
        if (sl.indexOf(query) >= 0) return 2;
        if (name === query) return 3;
        if (name.indexOf(query) === 0) return 4;
        if (name.indexOf(query) >= 0) return 5;
        return -1;
    }

    function filterAndRank(rawQuery) {
        var q = String(rawQuery || '').toLowerCase().trim();
        var assets = _getAllAssets();
        var wl = _watchlistSet();
        var all = Object.keys(assets);
        var favs = all.filter(function(s) { return wl[s]; });
        var others = all.filter(function(s) { return !wl[s]; });

        function rankSort(a, b) {
            var ra = matchRank(a, q), rb = matchRank(b, q);
            if (ra !== rb) return ra - rb;
            return a.localeCompare(b);
        }

        var filteredFavs = favs.filter(function(s) { return matchRank(s, q) >= 0; }).sort(rankSort);
        var filteredOthers = others.filter(function(s) { return matchRank(s, q) >= 0; }).sort(rankSort);
        return { favs: filteredFavs, others: filteredOthers, all: filteredFavs.concat(filteredOthers) };
    }

    function detectRatio(rawQuery) {
        var q = String(rawQuery || '').toLowerCase().trim();
        if (q.indexOf('/') <= 0) return null;
        var parts = q.split('/');
        if (parts.length !== 2) return null;
        var num = parts[0].trim().toUpperCase();
        var den = parts[1].trim().toUpperCase();
        var assets = _getAllAssets();
        if (!num || !den || num === den || !assets[num] || !assets[den]) return null;
        return { symbol: num + '_' + den, name: num + ' / ' + den + ' Ratio' };
    }

    function fetchDynamicSymbols(rawQuery, opts) {
        opts = opts || {};
        var q = encodeURIComponent(String(rawQuery || '').trim());
        var url = '/api/symbol-lookup?q=' + q + '&limit=' + (opts.limit || 8);
        var fetchOpts = {};
        if (opts.signal) fetchOpts.signal = opts.signal;
        return fetch(url, fetchOpts)
            .then(function(r) { return r.ok ? r.json() : []; })
            .then(function(data) {
                var seen = opts.seen || {};
                return (Array.isArray(data) ? data : []).filter(function(d) {
                    return d && d.tier === 'dynamic' && !seen[d.symbol];
                });
            });
    }

    // Yahoo exchange-suffix → market/currency hint for foreign listings
    // ("005930.KS" → Korea · KRW). Suffixes must survive symbol lookup
    // (stripping them makes non-US listings unloadable).
    var SUFFIX_REGIONS = {
        KS: { market: 'Korea', currency: 'KRW' }, KQ: { market: 'KOSDAQ', currency: 'KRW' },
        T: { market: 'Tokyo', currency: 'JPY' }, L: { market: 'London', currency: 'GBX' },
        IL: { market: 'London IOB', currency: 'USD' }, DE: { market: 'Xetra', currency: 'EUR' },
        F: { market: 'Frankfurt', currency: 'EUR' }, PA: { market: 'Paris', currency: 'EUR' },
        AS: { market: 'Amsterdam', currency: 'EUR' }, BR: { market: 'Brussels', currency: 'EUR' },
        MI: { market: 'Milan', currency: 'EUR' }, MC: { market: 'Madrid', currency: 'EUR' },
        LS: { market: 'Lisbon', currency: 'EUR' }, IR: { market: 'Dublin', currency: 'EUR' },
        VI: { market: 'Vienna', currency: 'EUR' }, HE: { market: 'Helsinki', currency: 'EUR' },
        SW: { market: 'Swiss', currency: 'CHF' }, ST: { market: 'Stockholm', currency: 'SEK' },
        CO: { market: 'Copenhagen', currency: 'DKK' }, OL: { market: 'Oslo', currency: 'NOK' },
        WA: { market: 'Warsaw', currency: 'PLN' }, PR: { market: 'Prague', currency: 'CZK' },
        BD: { market: 'Budapest', currency: 'HUF' }, IS: { market: 'Istanbul', currency: 'TRY' },
        TA: { market: 'Tel Aviv', currency: 'ILS' }, JO: { market: 'Johannesburg', currency: 'ZAR' },
        TO: { market: 'Toronto', currency: 'CAD' }, V: { market: 'TSX Venture', currency: 'CAD' },
        MX: { market: 'Mexico', currency: 'MXN' }, SA: { market: 'São Paulo', currency: 'BRL' },
        AX: { market: 'Australia', currency: 'AUD' }, NZ: { market: 'New Zealand', currency: 'NZD' },
        HK: { market: 'Hong Kong', currency: 'HKD' }, SS: { market: 'Shanghai', currency: 'CNY' },
        SZ: { market: 'Shenzhen', currency: 'CNY' }, TW: { market: 'Taiwan', currency: 'TWD' },
        NS: { market: 'NSE India', currency: 'INR' }, BO: { market: 'BSE India', currency: 'INR' },
        SI: { market: 'Singapore', currency: 'SGD' }, KL: { market: 'Malaysia', currency: 'MYR' },
        BK: { market: 'Bangkok', currency: 'THB' }, JK: { market: 'Jakarta', currency: 'IDR' }
    };

    function suffixRegion(symbol) {
        var s = String(symbol || '').toUpperCase();
        var dot = s.lastIndexOf('.');
        if (dot <= 0 || dot === s.length - 1) return null;
        var hit = SUFFIX_REGIONS[s.slice(dot + 1)];
        return hit ? { market: hit.market, currency: hit.currency } : null;
    }

    global.assetSearchCore = {
        matchRank: matchRank,
        filterAndRank: filterAndRank,
        detectRatio: detectRatio,
        fetchDynamicSymbols: fetchDynamicSymbols,
        suffixRegion: suffixRegion,
    };
})(typeof window !== 'undefined' ? window : this);
