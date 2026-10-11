/* Ticker Bar — configurable scrolling news ticker for the header bar */
// ============================================================
// Replaces the static sentiment-bar with a CNBC/Bloomberg-style
// scrolling ticker. Modules: sentiment, prices, alerts, positions.
// ============================================================

var tickerRefreshInterval = null;
var TICKER_REFRESH_MS = 5 * 60 * 1000; // 5 min for sentiment
var TICKER_ALERT_REFRESH_MS = 60 * 1000; // 1 min for alerts
var TICKER_POSITION_REFRESH_MS = 10 * 1000; // 10s for positions
var _tickerAnimationId = null;
var _tickerPriceIntervalId = null;
var _tickerAlertIntervalId = null;
var _tickerRebuildRafId = null;
var _tickerPaused = false;
var _tickerHardPaused = false; // Hard-pause (e.g. while chart modal is visible) — fully cancels rAF loop
var _tickerTotalScroll = 0;    // Saved at animation start so we can resume without re-measuring
var _tickerOffset = 0;
var _tickerSpeed = 0.5; // px per frame (updated from config)
var _tickerSentimentData = null;
var _tickerAlertData = [];
var _tickerLastAlertFetch = 0;
var _tickerLastPositionRefresh = 0;
var _tickerDragging = false;
var _tickerNewsData = null;
var _tickerLastNewsFetch = 0;
var TICKER_NEWS_REFRESH_MS = 300000; // 5 min

// ---- Config (localStorage) ----

var TICKER_DEFAULT_CONFIG = {
    modules: {
        sentiment: true,
        prices: true,
        alerts: true,
        positions: true,
        plays: false,
        customText: false,
        news: false,
        customRss: false
    },
    priceSymbols: ['BTC', 'ETH', 'SOL', 'XRP'],
    sentimentItems: ['regime', 'fg', 'mvrv', 'alt', 'ls', 'fr', 'oi', 'm2', 'ff', 'yc'],
    newsFeeds: ['coindesk', 'cointelegraph', 'decrypt'],
    newsMaxItems: 10,
    customFeeds: [], // [{url: 'https://...', label: 'My Blog', enabled: true}]
    speed: 'normal', // 'slow', 'normal', 'fast'
    customText: '' // Free text with basic markup: *bold*, _italic_, {#color}text{/}
};

function _tickerGetConfig() {
    try {
        var saved = JSON.parse(localStorage.getItem('tickerBarConfig'));
        if (saved) {
            // Merge with defaults for any new keys
            var cfg = JSON.parse(JSON.stringify(TICKER_DEFAULT_CONFIG));
            if (saved.modules) {
                for (var k in saved.modules) cfg.modules[k] = saved.modules[k];
            }
            if (saved.priceSymbols) cfg.priceSymbols = saved.priceSymbols;
            if (saved.sentimentItems) cfg.sentimentItems = saved.sentimentItems;
            if (saved.speed) cfg.speed = saved.speed;
            if (saved.customText !== undefined) cfg.customText = saved.customText;
            if (saved._lastColor) cfg._lastColor = saved._lastColor;
            if (saved.newsFeeds) cfg.newsFeeds = saved.newsFeeds;
            if (saved.newsMaxItems !== undefined) cfg.newsMaxItems = saved.newsMaxItems;
            if (saved.customFeeds) cfg.customFeeds = saved.customFeeds;
            return _tickerSanitizeConfig(cfg);
        }
    } catch(e) {}
    return JSON.parse(JSON.stringify(TICKER_DEFAULT_CONFIG));
}

var TICKER_CUSTOM_TEXT_MAX = 200;

function _tickerSanitizeConfig(cfg) {
    // Enforce custom text limit
    if (cfg.customText && cfg.customText.length > TICKER_CUSTOM_TEXT_MAX) {
        cfg.customText = cfg.customText.substring(0, TICKER_CUSTOM_TEXT_MAX);
    }
    // Strip anything dangerous from custom text (belt-and-suspenders)
    if (cfg.customText) {
        cfg.customText = cfg.customText
            .replace(/<[^>]*>/g, '')           // strip any HTML tags
            .replace(/javascript\s*:/gi, '')    // strip javascript: URIs
            .replace(/on\w+\s*=/gi, '')         // strip event handlers
            .replace(/expression\s*\(/gi, '')   // strip CSS expressions
            .replace(/url\s*\(/gi, '');          // strip CSS url()
    }
    // Cap price symbols to 20
    if (cfg.priceSymbols && cfg.priceSymbols.length > 20) {
        cfg.priceSymbols = cfg.priceSymbols.slice(0, 20);
    }
    // Sanitize price symbol names (uppercase alpha only, max 10 chars each)
    if (cfg.priceSymbols) {
        cfg.priceSymbols = cfg.priceSymbols
            .map(function(s) { return s.replace(/[^A-Z0-9]/g, '').substring(0, 10); })
            .filter(function(s) { return s.length > 0; });
    }
    // Validate news feeds against whitelist
    var _VALID_NEWS_FEEDS = ['cnbc_top','dlnews','coindesk','cointelegraph','theblock','decrypt','marketwatch','seekingalpha','fed','fxstreet'];
    if (cfg.newsFeeds) {
        cfg.newsFeeds = cfg.newsFeeds.filter(function(f) { return _VALID_NEWS_FEEDS.indexOf(f) !== -1; });
    }
    // Cap newsMaxItems 5-20
    if (cfg.newsMaxItems !== undefined) {
        cfg.newsMaxItems = Math.max(5, Math.min(20, parseInt(cfg.newsMaxItems) || 10));
    }
    // Sanitize custom feeds
    if (cfg.customFeeds) {
        cfg.customFeeds = cfg.customFeeds.filter(function(f) {
            return f && typeof f.url === 'string' && f.url.indexOf('https://') === 0;
        }).slice(0, 5).map(function(f) {
            return { url: f.url.substring(0, 500), label: String(f.label || '').substring(0, 30), enabled: !!f.enabled };
        });
    }
    return cfg;
}

function _tickerSaveConfig(cfg) {
    cfg = _tickerSanitizeConfig(cfg);
    try { localStorage.setItem('tickerBarConfig', JSON.stringify(cfg)); } catch(e) {}
    // Sync to server for cross-device / post-logout restore
    if (window.PreferenceSync) PreferenceSync.syncPreference('ticker_prefs', cfg);
}

// ---- Sentiment module ----

function _tickerFetchSentiment() {
    fetch('/api/market-sentiment', { headers: { 'Accept': 'application/json' } })
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(data) {
        if (data && data.status === 'ok' && data.sentiment) {
            _tickerSentimentData = data.sentiment;
            _tickerRebuild();
        }
    })
    .catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/market-sentiment failed — ticker sentiment stays empty', String((e && e.message) || e)); });
}

function _tickerSentimentItems(cfg) {
    var s = _tickerSentimentData;
    if (!s) return [];
    var items = [];
    var enabled = cfg.sentimentItems || [];

    if (enabled.indexOf('regime') !== -1 && s.regime) {
        items.push({ label: 'Regime', value: s.regime.regime || 'N/A', color: s.regime.color || '#8b949e', module: 'sentiment' });
    }
    // omit rather than print 'null Neutral'.
    if (enabled.indexOf('fg') !== -1 && s.fear_greed && s.fear_greed.value != null) {
        items.push({ label: 'F&G', value: s.fear_greed.value + ' ' + (s.fear_greed.classification || ''), color: s.fear_greed.color || '#c9d1d9', module: 'sentiment' });
    }
    if (enabled.indexOf('mvrv') !== -1 && s.mvrv_zscore && s.mvrv_zscore.zscore != null) {
        items.push({ label: 'MVRV-Z', value: s.mvrv_zscore.zscore.toFixed(1), color: s.mvrv_zscore.color || '#c9d1d9', module: 'sentiment' });
    }
    // `index` is null when unmeasurable — omit the item entirely,
    // as MVRV/L-S/FR/OI below already do. Math.round(null) is 0, not 'no data'.
    if (enabled.indexOf('alt') !== -1 && s.alt_season && s.alt_season.index != null) {
        items.push({ label: 'ASI', value: Math.round(s.alt_season.index).toString(), color: s.alt_season.color || '#c9d1d9', module: 'sentiment' });
    }
    if (enabled.indexOf('ls') !== -1 && s.long_short && s.long_short.BTC && s.long_short.BTC.ratio != null) {
        items.push({ label: 'L/S', value: s.long_short.BTC.ratio.toFixed(2), color: s.long_short.BTC.color || '#c9d1d9', module: 'sentiment' });
    }
    if (enabled.indexOf('fr') !== -1 && s.funding_rate && s.funding_rate.rate_pct != null) {
        items.push({ label: 'FR', value: s.funding_rate.rate_pct.toFixed(3) + '%', color: s.funding_rate.color || '#c9d1d9', module: 'sentiment' });
    }
    if (enabled.indexOf('oi') !== -1 && s.open_interest && s.open_interest.value_usd != null) {
        var oiColor = '#adb5bd';
        if (s.open_interest.change_pct > 0) oiColor = '#51cf66';
        else if (s.open_interest.change_pct < 0) oiColor = '#ff6b6b';
        items.push({ label: 'OI', value: '$' + _tickerFormatCompact(s.open_interest.value_usd), color: oiColor, module: 'sentiment' });
    }
    if (enabled.indexOf('m2') !== -1 && s.fred && s.fred.m2 && s.fred.m2.yoy_pct != null) {
        var m2 = s.fred.m2;
        var m2Color = m2.yoy_pct > 3 ? '#51cf66' : (m2.yoy_pct < 0 ? '#ff6b6b' : '#d29922');
        items.push({ label: 'M2', value: (m2.yoy_pct >= 0 ? '+' : '') + m2.yoy_pct.toFixed(1) + '%', color: m2Color, module: 'sentiment' });
    }
    if (enabled.indexOf('ff') !== -1 && s.fred && s.fred.fed_funds && s.fred.fed_funds.value != null) {
        var ff = s.fred.fed_funds;
        var ffTrend = ff.trend || 'flat';
        var ffColor = ffTrend === 'falling' ? '#51cf66' : (ffTrend === 'rising' ? '#ff6b6b' : '#d29922');
        items.push({ label: 'FFR', value: ff.value.toFixed(2) + '%', color: ffColor, module: 'sentiment' });
    }
    if (enabled.indexOf('yc') !== -1 && s.fred && s.fred.yield_spread && s.fred.yield_spread.value != null) {
        var yc = s.fred.yield_spread;
        var ycColor = yc.value < 0 ? '#ff6b6b' : (yc.value > 0.5 ? '#51cf66' : '#d29922');
        items.push({ label: '10Y-2Y', value: (yc.value >= 0 ? '+' : '') + yc.value.toFixed(2) + '%', color: ycColor, module: 'sentiment' });
    }
    return items;
}

// ---- Prices module ----

function _tickerPriceItems(cfg) {
    if (typeof allAssets === 'undefined') return [];
    var items = [];
    (cfg.priceSymbols || []).forEach(function(sym) {
        var asset = allAssets[sym];
        if (!asset || !asset.latest_price) return;
        var price = asset.latest_price;
        var change = asset.realtime ? asset.realtime.change_pct : null;
        var color = '#c9d1d9';
        var changeStr = '';
        if (change != null) {
            color = change >= 0 ? '#51cf66' : '#ff6b6b';
            changeStr = ' ' + (change >= 0 ? '+' : '') + change.toFixed(1) + '%';
        }
        items.push({
            label: sym,
            value: '$' + _tickerFormatPrice(price, sym) + changeStr,
            color: color,
            module: 'prices',
            symbol: sym
        });
    });
    return items;
}

// ---- Alerts module ----

function _tickerFetchAlerts() {
    var now = Date.now();
    if (now - _tickerLastAlertFetch < TICKER_ALERT_REFRESH_MS) return;
    _tickerLastAlertFetch = now;
    fetch('/api/alerts?active=true', { credentials: 'same-origin' })
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(data) {
        if (data && data.alerts) {
            _tickerAlertData = data.alerts.filter(function(a) { return !a.triggered; });
            _tickerRebuild();
        }
    })
    // SILENCE-OK: throttled poll (TICKER_ALERT_REFRESH_MS via _tickerLastAlertFetch)
    // — a miss just means the ticker's alert list is stale until the next cycle.
    .catch(function() {});
}

function _tickerAlertItems(cfg) {
    if (typeof allAssets === 'undefined') return [];
    var items = [];
    _tickerAlertData.forEach(function(alert) {
        // Skip alerts without a numeric price (trendline/indicator alerts)
        if (!alert.price || alert.price <= 0) return;
        // Skip non-price alert types
        if (alert.type && alert.type !== 'price') return;
        var asset = allAssets[alert.symbol];
        if (!asset || !asset.latest_price) return;
        var current = asset.latest_price;
        var target = alert.price;
        var distPct = ((target - current) / current * 100);
        var arrow = alert.condition === 'above' ? '\u25B2' : '\u25BC';
        var color = Math.abs(distPct) < 2 ? '#d29922' : '#8b949e';
        if (Math.abs(distPct) < 1) color = '#ff6b6b'; // very close
        items.push({
            label: alert.symbol + ' ' + arrow + '$' + _tickerFormatPrice(target, alert.symbol),
            value: (distPct >= 0 ? '+' : '') + distPct.toFixed(1) + '%',
            color: color,
            module: 'alerts'
        });
    });
    // Sort by distance (closest first)
    items.sort(function(a, b) {
        var aMatch = a.value.match(/([-+]?[\d.]+)%/);
        var bMatch = b.value.match(/([-+]?[\d.]+)%/);
        return Math.abs(parseFloat(aMatch ? aMatch[1] : 999)) - Math.abs(parseFloat(bMatch ? bMatch[1] : 999));
    });
    return items;
}

// ---- Positions module ----

function _tickerPositionItems(cfg) {
    if (typeof allAssets === 'undefined') return { futures: [], spots: [] };
    var futures = [];
    var spots = [];
    try {
        var allDrawings = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var symbols = Object.keys(allDrawings);
        symbols.forEach(function(sym) {
            var drawings = allDrawings[sym] || [];
            // Futures positions
            drawings.forEach(function(d) {
                if ((d.name === 'position' || d.name === 'longPosition' || d.name === 'shortPosition') && d.points && d.points.length >= 3) {
                    var entry = d.points[0].value;
                    var sl = d.points[1].value;
                    var isLong = sl < entry;
                    var asset = allAssets[sym];
                    var current = asset ? asset.latest_price : 0;
                    if (!current) return;
                    var plPct = ((current - entry) / entry * 100);
                    if (!isLong) plPct = -plPct;
                    var dir = isLong ? 'LONG' : 'SHORT';
                    var color = plPct >= 0 ? '#26a69a' : '#ef5350';
                    futures.push({
                        label: sym + ' ' + dir,
                        value: (plPct >= 0 ? '+' : '') + plPct.toFixed(1) + '%',
                        color: color,
                        module: 'positions'
                    });
                }
            });
            // Spot buys — aggregate per symbol
            var spotQty = 0;
            var spotCost = 0;
            drawings.forEach(function(d) {
                if (d.name === 'spot' && d.points && d.points.length >= 1 && !(d.extendData && d.extendData.sold)) {
                    var qty = (d.extendData || {}).quantity || 0;
                    var ep = d.points[0].value;
                    spotQty += qty;
                    spotCost += qty * ep;
                }
            });
            if (spotQty > 0) {
                var avgEntry = spotCost / spotQty;
                var asset = allAssets[sym];
                var current = asset ? asset.latest_price : 0;
                if (!current) return;
                var plPct = ((current - avgEntry) / avgEntry * 100);
                var value = current * spotQty;
                var color = plPct >= 0 ? '#4db6ac' : '#ef5350';
                spots.push({
                    label: sym,
                    value: '$' + _tickerFormatCompact(value) + ' ' + (plPct >= 0 ? '+' : '') + plPct.toFixed(1) + '%',
                    color: color,
                    module: 'positions'
                });
            }
        });
    } catch(e) {}
    return { futures: futures, spots: spots };
}

// ---- Custom text module ----

function _tickerParseCustomText(text) {
    if (!text) return '';
    // Enforce length limit
    if (text.length > TICKER_CUSTOM_TEXT_MAX) text = text.substring(0, TICKER_CUSTOM_TEXT_MAX);
    // Escape HTML
    var safe = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // Color: {#ff6b6b}text{/} — process innermost first, loop for nesting
    var limit = 5;
    while (limit-- > 0 && safe.indexOf('{#') !== -1 && safe.indexOf('{/}') !== -1) {
        safe = safe.replace(/\{#([0-9a-fA-F]{3,6})\}((?:(?!\{#)[\s\S])*?)\{\/\}/g, '<span style="color:#$1">$2</span>');
    }
    safe = safe.replace(/\*([^*]+)\*/g, '<strong>$1</strong>');
    safe = safe.replace(/_([^_]+)_/g, '<em>$1</em>');
    // Post-parse safety: only allow <span style="color:#hex">, <strong>, <em>, and their closing tags
    safe = safe.replace(/<(?!\/?(?:span|strong|em)\b)[^>]*>/gi, '');
    return safe;
}

function _tickerCustomTextItems(cfg) {
    if (!cfg.customText || !cfg.customText.trim()) return [];
    return [{ customHtml: _tickerParseCustomText(cfg.customText), module: 'customText' }];
}

// Simple string hash for custom feed URL → ID matching (mirrors backend md5[:8])
function _tickerHashUrl(url) {
    var hash = 0;
    for (var i = 0; i < url.length; i++) {
        hash = ((hash << 5) - hash) + url.charCodeAt(i);
        hash |= 0;
    }
    return Math.abs(hash).toString(16).substring(0, 8);
}

// ---- News module ----

var _NEWS_FEED_NAMES = {
    cnbc_top: 'CNBC', dlnews: 'DL News', coindesk: 'CoinDesk',
    cointelegraph: 'CoinTelegraph', theblock: 'The Block', decrypt: 'Decrypt',
    marketwatch: 'MarketWatch', seekingalpha: 'Seeking Alpha', fed: 'Fed', fxstreet: 'FXStreet'
};

function _tickerFetchNews() {
    var cfg = _tickerGetConfig();
    var hasBuiltin = cfg.modules.news && cfg.newsFeeds && cfg.newsFeeds.length;
    var customUrls = cfg.modules.customRss ? (cfg.customFeeds || []).filter(function(f) { return f.enabled; }).map(function(f) { return f.url; }) : [];
    if (!hasBuiltin && !customUrls.length) return;
    var now = Date.now();
    if (now - _tickerLastNewsFetch < TICKER_NEWS_REFRESH_MS && _tickerNewsData) return;
    _tickerLastNewsFetch = now;
    var builtinFeeds = hasBuiltin ? (cfg.newsFeeds || []).join(',') : '';
    var url = '/api/market-news?feeds=' + encodeURIComponent(builtinFeeds);
    if (customUrls.length) url += '&custom=' + encodeURIComponent(customUrls.join(','));
    fetch(url, { headers: { 'Accept': 'application/json' } })
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(data) {
        if (data && data.status === 'ok') {
            _tickerNewsData = data.items || [];
            _tickerRebuild();
        }
    })
    // SILENCE-OK: throttled poll (TICKER_NEWS_REFRESH_MS via _tickerLastNewsFetch)
    // — a miss just means the news strip stays on its last-known items.
    .catch(function() {});
}

function _tickerTimeAgo(isoStr) {
    if (!isoStr) return '';
    try {
        var d = new Date(isoStr);
        var now = new Date();
        var diff = Math.floor((now - d) / 1000);
        if (diff < 60) return 'now';
        if (diff < 3600) return Math.floor(diff / 60) + 'm';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h';
        return Math.floor(diff / 86400) + 'd';
    } catch(e) { return ''; }
}

// Plays module: shows active play status summary
var _tickerPlayData = null;
var _tickerPlayLastFetch = 0;

function _tickerPlayItems(cfg) {
    // Fetch play data (cache for 60s)
    var now = Date.now();
    if (!_tickerPlayData || now - _tickerPlayLastFetch > 60000) {
        _tickerPlayLastFetch = now;
        fetch('/api/plays/active', { credentials: 'same-origin' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                _tickerPlayData = data.plays || [];
                _tickerRebuild(); // Rebuild ticker with new data
            })
            .catch(function() { _tickerPlayData = []; });
    }
    if (!_tickerPlayData || !_tickerPlayData.length) return [];

    var statusSymbols = { acting: '\u26a0', stalking: '\u2192', drafting: '\u25cb', completed: '\u2713' };
    var statusColors = { acting: '#f0b90b', stalking: '#42a5f5', drafting: '#787b86', completed: '#26a69a' };

    return _tickerPlayData.slice(0, 8).map(function(p) {
        var status = p.status || 'drafting';
        var sym = statusSymbols[status] || '\u25cb';
        var color = statusColors[status] || '#787b86';
        var conds = p.conditions || {};
        var rules = conds.rules || [];
        var met = rules.filter(function(r) { return r.met; }).length;
        var total = rules.length;
        var condStr = total > 0 ? ' (' + met + '/' + total + ')' : '';
        return {
            text: sym + ' ' + (p.primary_asset || '?') + ' ' + (p.title || '').substring(0, 20) + condStr,
            color: color,
        };
    });
}

function _tickerNewsItems(cfg) {
    if (!_tickerNewsData || !_tickerNewsData.length) return [];
    var max = cfg.newsMaxItems || 10;
    var items = [];
    // Build custom feed label lookup
    var _customLabels = {};
    (cfg.customFeeds || []).forEach(function(f) {
        if (f.url && f.label) {
            var hash = _tickerHashUrl(f.url);
            _customLabels['custom_' + hash] = f.label;
        }
    });
    var slice = _tickerNewsData.slice(0, max);
    slice.forEach(function(n) {
        var ago = _tickerTimeAgo(n.published);
        var source = _customLabels[n.source_id] || n.source || 'News';
        var label = source + (ago ? ' ' + ago : '');
        // Truncate title to 80 chars
        var title = n.title || '';
        if (title.length > 80) title = title.substring(0, 77) + '...';
        items.push({
            label: label,
            value: title,
            color: '#58a6ff',
            module: 'news',
            newsData: n
        });
    });
    return items;
}

function _tickerShowNewsPreview(newsItem) {
    // Remove existing preview
    var existing = document.getElementById('news-preview-overlay');
    if (existing) existing.remove();

    var overlay = document.createElement('div');
    overlay.id = 'news-preview-overlay';
    overlay.className = 'news-preview-overlay';

    var ago = _tickerTimeAgo(newsItem.published);
    var safeTitle = escapeHtml(newsItem.title || 'Untitled');
    var safeSummary = escapeHtml(newsItem.summary || 'No summary available.');
    var safeSource = escapeHtml(newsItem.source || 'Unknown');
    var safeAgo = escapeHtml(ago);

    var linkHtml = '';
    if (newsItem.link && (newsItem.link.indexOf('https://') === 0 || newsItem.link.indexOf('http://') === 0)) {
        linkHtml = '<a href="' + escapeHtml(newsItem.link) + '" target="_blank" rel="noopener noreferrer" class="news-preview-link">Open Article \u2192</a>';
    }

    overlay.innerHTML =
        '<div class="news-preview-card">' +
            '<div class="news-preview-header">' +
                '<span class="news-preview-source">' + safeSource + '</span>' +
                '<span class="news-preview-time">' + safeAgo + '</span>' +
                '<button type="button" class="news-preview-close">\u2715</button>' +
            '</div>' +
            '<div class="news-preview-title">' + safeTitle + '</div>' +
            '<div class="news-preview-summary">' + safeSummary + '</div>' +
            linkHtml +
        '</div>';

    document.body.appendChild(overlay);

    // Close handlers
    overlay.querySelector('.news-preview-close').addEventListener('click', function() { overlay.remove(); });
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) overlay.remove();
    });
    document.addEventListener('keydown', function _newsEsc(e) {
        if (e.key === 'Escape') {
            var el = document.getElementById('news-preview-overlay');
            if (el) el.remove();
            document.removeEventListener('keydown', _newsEsc);
        }
    });
}

// ---- Rendering engine ----

function _tickerBuildItems() {
    var cfg = _tickerGetConfig();

    // Collect sections with their data first to count how many have content
    var sections = [];
    if (cfg.modules.customText) {
        var ct = _tickerCustomTextItems(cfg);
        if (ct.length) sections.push({ items: ct, module: 'customText' });
    }
    if (cfg.modules.sentiment) {
        var si = _tickerSentimentItems(cfg);
        if (si.length) sections.push({ header: 'SENTIMENT', items: si, module: 'sentiment' });
    }
    if (cfg.modules.prices) {
        var pi = _tickerPriceItems(cfg);
        if (pi.length) sections.push({ header: 'PRICES', items: pi, module: 'prices' });
    }
    if (cfg.modules.alerts) {
        var ai = _tickerAlertItems(cfg);
        if (ai.length) sections.push({ header: 'ALERTS', items: ai, module: 'alerts' });
    }
    if (cfg.modules.positions) {
        var posData = _tickerPositionItems(cfg);
        if (posData.futures.length) sections.push({ header: 'FUTURES', items: posData.futures, module: 'positions' });
        if (posData.spots.length) sections.push({ header: 'SPOTS', items: posData.spots, module: 'positions' });
    }
    if (cfg.modules.plays) {
        var pli = _tickerPlayItems(cfg);
        if (pli.length) sections.push({ header: 'PLAYS', items: pli, module: 'plays' });
    }
    if (cfg.modules.news) {
        var ni = _tickerNewsItems(cfg);
        if (ni.length) sections.push({ header: 'NEWS', items: ni, module: 'news' });
    }

    // Only show section headers when multiple sections have data
    var showHeaders = sections.filter(function(s) { return s.header; }).length > 1;

    var allItems = [];
    sections.forEach(function(sec, idx) {
        if (sec.header && showHeaders) {
            allItems.push({ sectionHeader: sec.header, module: sec.module });
        }
        allItems = allItems.concat(sec.items);
        if (idx < sections.length - 1) {
            allItems.push({ divider: true, afterModule: sec.module });
        }
    });
    // Remove trailing divider
    if (allItems.length && allItems[allItems.length - 1].divider) {
        allItems.pop();
    }
    return allItems;
}

function _tickerRenderItems(items) {
    var html = '';
    items.forEach(function(item) {
        if (item.divider) {
            html += '<span class="ticker-divider">\u2502</span>';
        } else if (item.sectionHeader) {
            html += '<span class="ticker-section-header">' + escapeHtml(item.sectionHeader) + '</span>';
        } else if (item.customHtml) {
            html += '<span class="ticker-item ticker-custom" data-module="customText">' + item.customHtml + '</span>';
        } else {
            var safeColor = _tickerEnsureVisible(item.color || '#c9d1d9');
            var extraAttr = '';
            var extraClass = '';
            if (item.module === 'news' && item.newsData) {
                extraAttr = ' data-news-id="' + escapeHtml(item.newsData.id || '') + '"';
                extraClass = ' ticker-news-item';
            }
            html += '<span class="ticker-item' + extraClass + '" data-module="' + (item.module || '') + '"' + extraAttr + '>' +
                '<span class="ticker-label">' + escapeHtml(item.label) + '</span>' +
                '<span class="ticker-value" style="color:' + safeColor + '">' + escapeHtml(item.value || '--') + '</span>' +
            '</span>';
        }
    });
    return html;
}

var _tickerLastItemKeys = '';
var _tickerIsScrolling = false;

function _tickerRebuild() {
    var track = document.getElementById('ticker-track');
    var container = document.getElementById('ticker-container');
    if (!track || !container) return;

    var items = _tickerBuildItems();
    if (items.length === 0) {
        track.innerHTML = '<span class="ticker-item"><span class="ticker-label" style="color:#8b949e">Loading ticker...</span></span>';
        _tickerStopAnimation();
        _tickerLastItemKeys = '';
        return;
    }

    // Build a key from item labels to detect structural changes
    var keys = items.map(function(it) { return it.divider ? '|' : (it.sectionHeader ? '#' + it.sectionHeader : (it.customHtml ? 'CT' : it.label)); }).join(',');

    // If structure unchanged and scrolling, do in-place value updates only
    if (keys === _tickerLastItemKeys && _tickerIsScrolling) {
        _tickerUpdateValuesInPlace(items);
        return;
    }
    _tickerLastItemKeys = keys;

    var html = _tickerRenderItems(items);

    // Measure if we need scrolling: render once, check width
    track.innerHTML = html;
    track.style.transform = 'translateX(0)';
    track.classList.remove('ticker-scrolling');

    // Wait a frame for layout
    if (_tickerRebuildRafId) cancelAnimationFrame(_tickerRebuildRafId);
    _tickerRebuildRafId = requestAnimationFrame(function() {
        var trackW = track.scrollWidth;
        var containerW = container.offsetWidth;

        if (trackW > containerW) {
            // Needs scrolling — duplicate content for seamless loop
            track.innerHTML = html + '<span class="ticker-divider ticker-loop-gap">\u2502</span>' + html;
            track.classList.add('ticker-scrolling');
            _tickerIsScrolling = true;
            _tickerStartAnimation(trackW);
        } else {
            // Fits — static display, center it
            track.classList.remove('ticker-scrolling');
            _tickerIsScrolling = false;
            _tickerStopAnimation();
        }
    });
}

function _tickerUpdateValuesInPlace(items) {
    var track = document.getElementById('ticker-track');
    if (!track) return;
    var valueEls = track.querySelectorAll('.ticker-value');
    // Only items with label+value produce .ticker-value elements
    var dataItems = items.filter(function(it) { return it.label && it.value !== undefined; });
    var n = dataItems.length;
    if (n === 0) return;
    for (var i = 0; i < valueEls.length; i++) {
        var item = dataItems[i % n];
        if (!item) continue;
        var val = item.value || '--';
        var col = _tickerEnsureVisible(item.color || '#c9d1d9');
        if (typeof CarracTick !== 'undefined') {
            /* Tick-color paint handles direction coloring + flash.
             * The ticker strip uses full-brightness colors (no tk-soft). */
            CarracTick.paint(valueEls[i], val);
        } else {
            valueEls[i].textContent = val;
            valueEls[i].style.color = col;
        }
    }
}

function _tickerApplySpeed() {
    var cfg = _tickerGetConfig();
    _tickerSpeed = cfg.speed === 'slow' ? 0.15 : (cfg.speed === 'fast' ? 0.8 : 0.4);
}

function _tickerStartAnimation(contentWidth) {
    _tickerStopAnimation();
    var track = document.getElementById('ticker-track');
    if (!track) return;

    _tickerApplySpeed();

    // Gap between the two copies (divider width ~20px)
    var gapWidth = 20;
    _tickerTotalScroll = contentWidth + gapWidth;
    _tickerOffset = 0;

    // Don't start the rAF loop while hard-paused (modal open). resumeTickerHard() will kick it.
    if (_tickerHardPaused) return;

    _tickerKickStepLoop();
}

// Extracted so resumeTickerHard() can restart the loop without re-measuring the track
function _tickerKickStepLoop() {
    // Skip JS animation when user prefers reduced motion
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var track = document.getElementById('ticker-track');
    if (!track) return;

    function step() {
        // Hard-pause: truly stop — do NOT reschedule rAF (was burning a mainthread slot per frame)
        if (_tickerHardPaused) {
            _tickerAnimationId = null;
            return;
        }
        // Tab hidden: stop the loop; visibilitychange listener will restart it
        if (document.hidden) {
            _tickerAnimationId = null;
            return;
        }
        if (!_tickerPaused) {
            _tickerOffset -= _tickerSpeed;
            if (Math.abs(_tickerOffset) >= _tickerTotalScroll) {
                _tickerOffset = 0;
            }
            track.style.transform = 'translateX(' + _tickerOffset + 'px)';
        }
        _tickerAnimationId = requestAnimationFrame(step);
    }
    _tickerAnimationId = requestAnimationFrame(step);
}

// Restart the rAF loop when the tab becomes visible again (once, guarded against double-bind)
if (!window._tickerVisibilityListenerAttached) {
    window._tickerVisibilityListenerAttached = true;
    document.addEventListener('visibilitychange', function() {
        if (!document.hidden && _tickerIsScrolling && !_tickerHardPaused && !_tickerAnimationId) {
            _tickerKickStepLoop();
        }
    });
}

function _tickerStopAnimation() {
    if (_tickerAnimationId) {
        cancelAnimationFrame(_tickerAnimationId);
        _tickerAnimationId = null;
    }
}

// --- Hard pause/resume (used while chart modal is visible) ---
// The ticker sits in the page header behind the fullscreen chart modal. Even though
// it's occluded, its rAF loop keeps mutating `transform` on a will-change layer 60x/sec.
// On mobile, that compositor churn competes with KLineChart's touch-pan redraws AND
// forces `.adv-chart-overlay`'s backdrop-filter blur to re-run per frame. Result: choppy pan.
// Hard-pause during modal restores the golden-standard silky pan.
window.pauseTickerHard = function() {
    if (_tickerHardPaused) return;
    _tickerHardPaused = true;
    _tickerStopAnimation();
};

window.resumeTickerHard = function() {
    if (!_tickerHardPaused) return;
    _tickerHardPaused = false;
    // Only re-kick if the ticker was actually scrolling (has content wider than container)
    if (_tickerIsScrolling && _tickerTotalScroll > 0 && !_tickerAnimationId) {
        _tickerKickStepLoop();
    }
};

// ---- Live price update (throttled, called from interval) ----

var _tickerPriceUpdateTimer = null;

function _tickerSchedulePriceRefresh() {
    // Throttle price-driven rebuilds to every 2 seconds
    if (_tickerPriceUpdateTimer) return;
    _tickerPriceUpdateTimer = setTimeout(function() {
        _tickerPriceUpdateTimer = null;
        _tickerRebuild();
    }, 2000);
}

// ---- Config popover ----

var TICKER_PALETTE = ['#ff6b6b','#ff922b','#fcc419','#51cf66','#20c997','#3bc9db','#339af0','#5c7cfa','#845ef7','#e64980','#c9d1d9','#8b949e'];

function _tickerOpenConfig(e) {
    if (e) e.stopPropagation();
    var existing = document.getElementById('ticker-config-popover');
    if (existing) { existing.remove(); return; }

    var cfg = _tickerGetConfig();
    var popover = document.createElement('div');
    popover.id = 'ticker-config-popover';
    popover.className = 'ticker-config-popover';

    var sentimentNames = {
        regime: 'Regime', fg: 'Fear & Greed', mvrv: 'MVRV Z-Score', alt: 'Alt Season',
        ls: 'Long/Short', fr: 'Funding Rate', oi: 'Open Interest',
        m2: 'M2 Supply', ff: 'Fed Funds', yc: 'Yield Curve'
    };
    var enabledCount = cfg.sentimentItems.length;

    var html = '<div class="tc-header">Ticker Settings</div>';

    // ── Modules as compact toggle chips ──
    html += '<div class="tc-section-label">Modules</div>';
    html += '<div class="tc-chips">';
    var mods = [
        { key: 'sentiment', label: 'Sentiment', icon: '\u2261' },
        { key: 'prices', label: 'Prices', icon: '$' },
        { key: 'alerts', label: 'Alerts', icon: '\u25B2' },
        { key: 'positions', label: 'Positions', icon: '\u25CF' },
        { key: 'plays', label: 'Plays', icon: '\u21C4' },
        { key: 'customText', label: 'Text', icon: 'T' },
        { key: 'news', label: 'News', icon: '\u2139' },
        { key: 'customRss', label: 'RSS', icon: '\u{1F4E1}' }
    ];
    var GATED_MODULES = ['plays', 'customText', 'news', 'customRss'];
    var isPremium = window._isPremium;
    mods.forEach(function(m) {
        var gated = !isPremium && GATED_MODULES.indexOf(m.key) !== -1;
        var active = cfg.modules[m.key] && !gated ? ' tc-chip-active' : '';
        var locked = gated ? ' tc-chip-locked' : '';
        html += '<button type="button" class="tc-chip' + active + locked + '" data-module="' + m.key + '"' +
            (gated ? ' data-gated="1"' : '') + '>' +
            '<span class="tc-chip-icon">' + (gated ? '\uD83D\uDD12' : m.icon) + '</span>' + m.label + '</button>';
    });
    html += '</div>';

    // ── Sentiment items — collapsible ──
    html += '<div class="tc-expandable" id="tc-sentiment-block" style="' + (cfg.modules.sentiment ? '' : 'display:none') + '">';
    html += '<button type="button" class="tc-expand-btn" id="tc-sentiment-toggle">';
    html += 'Sentiment Items <span class="tc-expand-count">' + enabledCount + '/10</span>';
    html += '<span class="tc-expand-arrow" id="tc-sentiment-arrow">\u25B6</span></button>';
    html += '<div class="tc-expand-body" id="tc-sentiment-body" style="display:none">';
    for (var si in sentimentNames) {
        html += '<label class="tc-toggle-row"><input type="checkbox" data-sentiment="' + si + '" ' +
            (cfg.sentimentItems.indexOf(si) !== -1 ? 'checked' : '') + '><span>' + sentimentNames[si] + '</span></label>';
    }
    html += '</div></div>';

    // ── News feeds — collapsible (premium only) ──
    var newsVisible = cfg.modules.news && isPremium;
    var newsFeedCount = cfg.newsFeeds ? cfg.newsFeeds.length : 0;
    html += '<div class="tc-expandable" id="tc-news-block" style="' + (newsVisible ? '' : 'display:none') + '">';
    html += '<button type="button" class="tc-expand-btn" id="tc-news-toggle">';
    html += 'News Feeds <span class="tc-expand-count">' + newsFeedCount + '/10</span>';
    html += '<span class="tc-expand-arrow" id="tc-news-arrow">\u25B6</span></button>';
    html += '<div class="tc-expand-body" id="tc-news-body" style="display:none">';
    var allFeeds = ['cnbc_top','dlnews','coindesk','cointelegraph','theblock','decrypt','marketwatch','seekingalpha','fed','fxstreet'];
    allFeeds.forEach(function(fid) {
        var fname = _NEWS_FEED_NAMES[fid] || fid;
        var checked = cfg.newsFeeds && cfg.newsFeeds.indexOf(fid) !== -1 ? 'checked' : '';
        html += '<label class="tc-toggle-row"><input type="checkbox" data-newsfeed="' + fid + '" ' + checked + '><span>' + escapeHtml(fname) + '</span></label>';
    });
    html += '</div></div>';

    // ── Custom RSS Feeds — own module, premium only ──
    if (isPremium) {
        var customRssVisible = cfg.modules.customRss;
        var customCount = (cfg.customFeeds || []).length;
        html += '<div class="tc-expandable" id="tc-custom-rss-block" style="' + (customRssVisible ? '' : 'display:none') + '">';
        html += '<button type="button" class="tc-expand-btn" id="tc-custom-rss-toggle">';
        html += 'Custom RSS Feeds <span class="tc-expand-count">' + customCount + '/5</span>';
        html += '<span class="tc-expand-arrow" id="tc-custom-rss-arrow">\u25B6</span></button>';
        html += '<div class="tc-expand-body" id="tc-custom-rss-body" style="display:none">';
        // Add feed input row
        html += '<div class="tc-custom-feed-input">';
        html += '<input type="text" class="tc-input tc-custom-url" id="tc-custom-url" placeholder="https://example.com/rss" style="margin-bottom:4px;">';
        html += '<div style="display:flex;gap:4px;align-items:center;">';
        html += '<input type="text" class="tc-input tc-custom-label" id="tc-custom-label" placeholder="Feed name" style="flex:1;">';
        html += '<button type="button" class="tc-test-btn" id="tc-custom-test">Test</button>';
        html += '<button type="button" class="tc-test-btn tc-add-btn" id="tc-custom-add" disabled>+ Add</button>';
        html += '</div>';
        html += '<div class="tc-test-result" id="tc-test-result"></div>';
        html += '</div>';
        // Saved feeds list
        (cfg.customFeeds || []).forEach(function(f, idx) {
            html += '<div class="tc-custom-feed-item" data-idx="' + idx + '">';
            html += '<label class="tc-toggle-row"><input type="checkbox" data-customfeed="' + idx + '"' + (f.enabled ? ' checked' : '') + '>';
            html += '<span>' + escapeHtml(f.label || f.url) + '</span></label>';
            html += '<button type="button" class="tc-custom-remove" data-removefeed="' + idx + '" title="Remove">\u2715</button>';
            html += '</div>';
        });
        html += '</div></div>';
    }

    // ── Price symbols ──
    html += '<div id="tc-prices-block" style="' + (cfg.modules.prices ? '' : 'display:none') + '">';
    html += '<div class="tc-section-label">Price Symbols</div>';
    html += '<input type="text" class="tc-input" id="tc-price-symbols" value="' + escapeHtml(cfg.priceSymbols.join(', ')) + '" placeholder="BTC, ETH, SOL...">';
    html += '</div>';

    // ── Speed — segmented control ──
    html += '<div class="tc-section-label">Speed</div>';
    html += '<div class="tc-segmented">';
    ['slow', 'normal', 'fast'].forEach(function(sp) {
        var sel = cfg.speed === sp ? ' tc-seg-active' : '';
        html += '<button type="button" class="tc-seg' + sel + '" data-speed="' + sp + '">' + sp.charAt(0).toUpperCase() + sp.slice(1) + '</button>';
    });
    html += '</div>';

    // ── Custom text — with live preview, formatting toolbar ──
    html += '<div id="tc-custom-block" style="' + (cfg.modules.customText ? '' : 'display:none') + '">';
    html += '<div class="tc-section-label">Custom Text</div>';
    // Toolbar
    html += '<div class="tc-toolbar">';
    html += '<button type="button" class="tc-tool-btn" data-fmt="bold" title="Bold (select text or insert at cursor)"><strong>B</strong></button>';
    html += '<button type="button" class="tc-tool-btn" data-fmt="italic" title="Italic"><em>I</em></button>';
    html += '<div class="tc-tool-sep"></div>';
    // Color picker trigger
    html += '<div class="tc-color-picker-wrap">';
    html += '<button type="button" class="tc-tool-btn tc-color-trigger" id="tc-color-trigger" title="Text color">';
    html += '<span class="tc-color-swatch" id="tc-color-swatch" style="background:' + (cfg._lastColor || '#51cf66') + '"></span>A</button>';
    html += '<div class="tc-color-dropdown" id="tc-color-dropdown" style="display:none">';
    TICKER_PALETTE.forEach(function(c) {
        html += '<button type="button" class="tc-color-dot" data-color="' + c + '" style="background:' + c + '"></button>';
    });
    html += '</div></div>';
    html += '</div>';
    // Textarea
    html += '<textarea class="tc-textarea" id="tc-custom-text" rows="2" maxlength="' + TICKER_CUSTOM_TEXT_MAX + '" placeholder="Stay disciplined. Trust the process.">' + escapeHtml(cfg.customText || '') + '</textarea>';
    var curLen = (cfg.customText || '').length;
    html += '<div class="tc-char-count" id="tc-char-count">' + curLen + '/' + TICKER_CUSTOM_TEXT_MAX + '</div>';
    // Live preview
    html += '<div class="tc-preview" id="tc-preview"></div>';
    html += '</div>';

    popover.innerHTML = html;

    // Append to body
    var gear = document.getElementById('ticker-config-btn');
    document.body.appendChild(popover);
    if (gear) {
        var rect = gear.getBoundingClientRect();
        popover.style.top = (rect.bottom + 4) + 'px';
        popover.style.right = (window.innerWidth - rect.right) + 'px';
    }
    popover.addEventListener('click', function(ev) { ev.stopPropagation(); });

    // ── Update preview ──
    function updatePreview() {
        var prev = popover.querySelector('#tc-preview');
        var ta = popover.querySelector('#tc-custom-text');
        if (prev && ta) {
            var parsed = _tickerParseCustomText(ta.value);
            prev.innerHTML = parsed || '<span style="color:#484f58">Preview appears here</span>';
        }
    }
    updatePreview();

    // ── Module chips ──
    popover.querySelectorAll('.tc-chip').forEach(function(chip) {
        chip.addEventListener('click', function(ev) {
            ev.preventDefault();
            // Tier gate: show upgrade modal for locked modules
            if (chip.dataset.gated === '1') {
                if (typeof showUpgradeModal === 'function') {
                    showUpgradeModal('Ticker modules Plays, Custom Text, and News require a subscription.');
                }
                return;
            }
            var mod = chip.dataset.module;
            var cfg = _tickerGetConfig();
            cfg.modules[mod] = !cfg.modules[mod];
            chip.classList.toggle('tc-chip-active', cfg.modules[mod]);
            // Show/hide dependent blocks
            var blocks = { sentiment: 'tc-sentiment-block', prices: 'tc-prices-block', customText: 'tc-custom-block', news: 'tc-news-block', customRss: 'tc-custom-rss-block' };
            if (blocks[mod]) {
                var el = document.getElementById(blocks[mod]);
                if (el) el.style.display = cfg.modules[mod] ? '' : 'none';
            }
            _tickerSaveConfig(cfg);
            if ((mod === 'news' && cfg.modules.news) || (mod === 'customRss' && cfg.modules.customRss)) {
                _tickerLastNewsFetch = 0;
                _tickerFetchNews();
            }
            _tickerRebuild();
        });
    });

    // ── Sentiment expand/collapse ──
    var sentToggle = popover.querySelector('#tc-sentiment-toggle');
    if (sentToggle) {
        sentToggle.addEventListener('click', function() {
            var body = document.getElementById('tc-sentiment-body');
            var arrow = document.getElementById('tc-sentiment-arrow');
            if (body) {
                var open = body.style.display !== 'none';
                body.style.display = open ? 'none' : '';
                if (arrow) arrow.textContent = open ? '\u25B6' : '\u25BC';
            }
        });
    }

    // ── Sentiment checkboxes ──
    popover.querySelectorAll('[data-sentiment]').forEach(function(cb) {
        cb.addEventListener('change', function() {
            var cfg = _tickerGetConfig();
            var si = cb.dataset.sentiment;
            var idx = cfg.sentimentItems.indexOf(si);
            if (cb.checked && idx === -1) cfg.sentimentItems.push(si);
            else if (!cb.checked && idx !== -1) cfg.sentimentItems.splice(idx, 1);
            // Update count
            var cnt = popover.querySelector('.tc-expand-count');
            if (cnt) cnt.textContent = cfg.sentimentItems.length + '/10';
            _tickerSaveConfig(cfg);
            _tickerRebuild();
        });
    });

    // ── News expand/collapse ──
    var newsToggle = popover.querySelector('#tc-news-toggle');
    if (newsToggle) {
        newsToggle.addEventListener('click', function() {
            var body = document.getElementById('tc-news-body');
            var arrow = document.getElementById('tc-news-arrow');
            if (body) {
                var open = body.style.display !== 'none';
                body.style.display = open ? 'none' : '';
                if (arrow) arrow.textContent = open ? '\u25B6' : '\u25BC';
            }
        });
    }

    // ── News feed checkboxes ──
    popover.querySelectorAll('[data-newsfeed]').forEach(function(cb) {
        cb.addEventListener('change', function() {
            var cfg = _tickerGetConfig();
            var fid = cb.dataset.newsfeed;
            if (!cfg.newsFeeds) cfg.newsFeeds = [];
            var idx = cfg.newsFeeds.indexOf(fid);
            if (cb.checked && idx === -1) cfg.newsFeeds.push(fid);
            else if (!cb.checked && idx !== -1) cfg.newsFeeds.splice(idx, 1);
            var cnt = popover.querySelector('#tc-news-block .tc-expand-count');
            if (cnt) cnt.textContent = cfg.newsFeeds.length + '/10';
            _tickerSaveConfig(cfg);
            // Force re-fetch with new feeds
            _tickerLastNewsFetch = 0;
            _tickerNewsData = null;
            _tickerFetchNews();
        });
    });

    // ── Custom RSS expand/collapse ──
    var rssToggle = popover.querySelector('#tc-custom-rss-toggle');
    if (rssToggle) {
        rssToggle.addEventListener('click', function() {
            var body = document.getElementById('tc-custom-rss-body');
            var arrow = document.getElementById('tc-custom-rss-arrow');
            if (body) {
                var open = body.style.display !== 'none';
                body.style.display = open ? 'none' : '';
                if (arrow) arrow.textContent = open ? '\u25B6' : '\u25BC';
            }
        });
    }

    // ── Custom RSS: Test button ──
    var testBtn = popover.querySelector('#tc-custom-test');
    var addBtn = popover.querySelector('#tc-custom-add');
    if (testBtn) {
        testBtn.addEventListener('click', function() {
            var urlInput = popover.querySelector('#tc-custom-url');
            var labelInput = popover.querySelector('#tc-custom-label');
            var resultEl = popover.querySelector('#tc-test-result');
            var url = (urlInput ? urlInput.value : '').trim();
            if (!url || url.indexOf('https://') !== 0) {
                if (resultEl) { resultEl.textContent = '\u2717 URL must start with https://'; resultEl.className = 'tc-test-result tc-test-error'; }
                return;
            }
            testBtn.disabled = true;
            testBtn.textContent = '...';
            if (resultEl) { resultEl.textContent = 'Testing...'; resultEl.className = 'tc-test-result'; }
            fetch('/api/market-news/test', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': (window.getCSRFToken && window.getCSRFToken()) || '',
                },
                credentials: 'same-origin',
                body: JSON.stringify({ url: url })
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                testBtn.disabled = false;
                testBtn.textContent = 'Test';
                if (data.status === 'ok') {
                    if (resultEl) { resultEl.textContent = '\u2713 ' + data.item_count + ' items found'; resultEl.className = 'tc-test-result tc-test-ok'; }
                    if (labelInput && !labelInput.value.trim() && data.title) labelInput.value = data.title;
                    if (addBtn) { addBtn.disabled = false; addBtn._testedUrl = url; }
                } else {
                    if (resultEl) { resultEl.textContent = '\u2717 ' + (data.error || 'Unknown error'); resultEl.className = 'tc-test-result tc-test-error'; }
                    if (addBtn) addBtn.disabled = true;
                }
            })
            .catch(function() {
                testBtn.disabled = false;
                testBtn.textContent = 'Test';
                if (resultEl) { resultEl.textContent = '\u2717 Network error'; resultEl.className = 'tc-test-result tc-test-error'; }
            });
        });
    }

    // ── Custom RSS: Add button ──
    if (addBtn) {
        addBtn.addEventListener('click', function() {
            var urlInput = popover.querySelector('#tc-custom-url');
            var labelInput = popover.querySelector('#tc-custom-label');
            var url = addBtn._testedUrl || (urlInput ? urlInput.value.trim() : '');
            var label = (labelInput ? labelInput.value.trim() : '') || url.replace(/^https?:\/\//, '').split('/')[0];
            if (!url) return;
            var cfg = _tickerGetConfig();
            if (!cfg.customFeeds) cfg.customFeeds = [];
            if (cfg.customFeeds.length >= 5) return;
            // Deduplicate
            for (var i = 0; i < cfg.customFeeds.length; i++) {
                if (cfg.customFeeds[i].url === url) return;
            }
            cfg.customFeeds.push({ url: url, label: label.substring(0, 30), enabled: true });
            _tickerSaveConfig(cfg);
            // Re-fetch news and close/reopen popover to refresh list
            _tickerLastNewsFetch = 0;
            _tickerNewsData = null;
            _tickerFetchNews();
            // Refresh popover
            popover.remove();
            _tickerOpenConfig();
        });
    }

    // ── Custom RSS: Enable/disable checkboxes ──
    popover.querySelectorAll('[data-customfeed]').forEach(function(cb) {
        cb.addEventListener('change', function() {
            var idx = parseInt(cb.dataset.customfeed);
            var cfg = _tickerGetConfig();
            if (cfg.customFeeds && cfg.customFeeds[idx]) {
                cfg.customFeeds[idx].enabled = cb.checked;
                _tickerSaveConfig(cfg);
                _tickerLastNewsFetch = 0;
                _tickerNewsData = null;
                _tickerFetchNews();
            }
        });
    });

    // ── Custom RSS: Remove buttons ──
    popover.querySelectorAll('[data-removefeed]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var idx = parseInt(btn.dataset.removefeed);
            var cfg = _tickerGetConfig();
            if (cfg.customFeeds) {
                cfg.customFeeds.splice(idx, 1);
                _tickerSaveConfig(cfg);
                _tickerLastNewsFetch = 0;
                _tickerNewsData = null;
                _tickerFetchNews();
                // Refresh popover
                popover.remove();
                _tickerOpenConfig();
            }
        });
    });

    // ── Price symbols ──
    var priceInput = popover.querySelector('#tc-price-symbols');
    if (priceInput) {
        priceInput.addEventListener('input', function() {
            var cfg = _tickerGetConfig();
            cfg.priceSymbols = this.value.split(',').map(function(s) { return s.trim().toUpperCase(); }).filter(function(s) { return s.length > 0; });
            _tickerSaveConfig(cfg);
            _tickerRebuild();
        });
    }

    // ── Speed segmented control ──
    popover.querySelectorAll('.tc-seg').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var cfg = _tickerGetConfig();
            cfg.speed = btn.dataset.speed;
            popover.querySelectorAll('.tc-seg').forEach(function(b) { b.classList.remove('tc-seg-active'); });
            btn.classList.add('tc-seg-active');
            _tickerSaveConfig(cfg);
            _tickerApplySpeed();
        });
    });

    // ── Custom text textarea ──
    var customInput = popover.querySelector('#tc-custom-text');
    function _updateCharCount() {
        var cc = popover.querySelector('#tc-char-count');
        var ta = popover.querySelector('#tc-custom-text');
        if (cc && ta) cc.textContent = ta.value.length + '/' + TICKER_CUSTOM_TEXT_MAX;
    }
    if (customInput) {
        customInput.addEventListener('input', function() {
            var cfg = _tickerGetConfig();
            cfg.customText = this.value;
            _tickerSaveConfig(cfg);
            _tickerRebuild();
            updatePreview();
            _updateCharCount();
        });
    }

    // ── Format helper: works with or without selection ──
    function _insertOrWrap(ta, openTag, closeTag) {
        var start = ta.selectionStart;
        var end = ta.selectionEnd;
        var text = ta.value;
        if (start === end) {
            // No selection: insert tags at cursor, place cursor between
            ta.value = text.substring(0, start) + openTag + closeTag + text.substring(end);
            ta.focus();
            ta.selectionStart = ta.selectionEnd = start + openTag.length;
        } else {
            // Wrap selection
            var selected = text.substring(start, end);
            // Toggle off if already wrapped
            if (selected.startsWith(openTag) && selected.endsWith(closeTag)) {
                var unwrapped = selected.slice(openTag.length, -closeTag.length);
                ta.value = text.substring(0, start) + unwrapped + text.substring(end);
                ta.focus();
                ta.selectionStart = start;
                ta.selectionEnd = start + unwrapped.length;
            } else {
                ta.value = text.substring(0, start) + openTag + selected + closeTag + text.substring(end);
                ta.focus();
                ta.selectionStart = start;
                ta.selectionEnd = start + openTag.length + selected.length + closeTag.length;
            }
        }
        var cfg = _tickerGetConfig();
        cfg.customText = ta.value;
        _tickerSaveConfig(cfg);
        _tickerRebuild();
        updatePreview();
        _updateCharCount();
    }

    // ── B / I buttons ──
    popover.querySelectorAll('.tc-tool-btn[data-fmt]').forEach(function(btn) {
        btn.addEventListener('click', function(ev) {
            ev.preventDefault();
            ev.stopPropagation();
            var ta = popover.querySelector('#tc-custom-text');
            if (!ta) return;
            if (btn.dataset.fmt === 'bold') _insertOrWrap(ta, '*', '*');
            else if (btn.dataset.fmt === 'italic') _insertOrWrap(ta, '_', '_');
        });
    });

    // ── Color picker dropdown ──
    var colorTrigger = popover.querySelector('#tc-color-trigger');
    var colorDropdown = popover.querySelector('#tc-color-dropdown');
    if (colorTrigger && colorDropdown) {
        colorTrigger.addEventListener('click', function(ev) {
            ev.preventDefault();
            ev.stopPropagation();
            colorDropdown.style.display = colorDropdown.style.display === 'none' ? '' : 'none';
        });
        popover.querySelectorAll('.tc-color-dot').forEach(function(dot) {
            dot.addEventListener('click', function(ev) {
                ev.preventDefault();
                ev.stopPropagation();
                var color = dot.dataset.color;
                // Update swatch
                var swatch = popover.querySelector('#tc-color-swatch');
                if (swatch) swatch.style.background = color;
                colorDropdown.style.display = 'none';
                // Save last color
                var cfg = _tickerGetConfig();
                cfg._lastColor = color;
                _tickerSaveConfig(cfg);
                // Apply to textarea
                var ta = popover.querySelector('#tc-custom-text');
                if (ta) _insertOrWrap(ta, '{' + color + '}', '{/}');
            });
        });
    }

    // Close on outside click (synchronous — gear click uses stopPropagation)
    document.addEventListener('click', _tickerCloseConfigOutside);
}

function _tickerCloseConfigOutside(e) {
    var popover = document.getElementById('ticker-config-popover');
    var gear = document.getElementById('ticker-config-btn');
    if (popover && !popover.contains(e.target) && !(gear && gear.contains(e.target))) {
        popover.remove();
        document.removeEventListener('click', _tickerCloseConfigOutside);
    }
}

// ---- Helpers ----

// Ensure a hex color is bright enough to read on dark (#161b22) background
function _tickerEnsureVisible(color) {
    if (!color || color.charAt(0) !== '#') return '#c9d1d9';
    var hex = color.replace('#', '');
    if (hex.length === 3) hex = hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2];
    var r = parseInt(hex.substring(0, 2), 16);
    var g = parseInt(hex.substring(2, 4), 16);
    var b = parseInt(hex.substring(4, 6), 16);
    // Relative luminance (simplified)
    var lum = (0.299 * r + 0.587 * g + 0.114 * b);
    // If too dark to read on ~#161b22 background, lighten
    if (lum < 80) return '#c9d1d9';
    return color;
}

function _tickerFormatCompact(num) {
    if (num == null) return '--';
    if (num >= 1e9) return (num / 1e9).toFixed(1) + 'B';
    if (num >= 1e6) return (num / 1e6).toFixed(1) + 'M';
    if (num >= 1e3) return (num / 1e3).toFixed(1) + 'K';
    return num.toFixed(0);
}

function _tickerFormatPrice(price, symbol) {
    if (price == null) return '--';
    if (price >= 1000) return price.toLocaleString(undefined, { maximumFractionDigits: 0 });
    if (price >= 1) return price.toFixed(2);
    if (price >= 0.01) return price.toFixed(4);
    return price.toFixed(6);
}


// ---- Init ----

function initTickerBar() {
    // Fetch sentiment immediately
    _tickerFetchSentiment();

    // Fetch alerts
    _tickerFetchAlerts();

    // Fetch news
    _tickerFetchNews();

    // Initial render — defer slightly longer if no localStorage config (lets prefs-sync restore from server first)
    var _hasLocalTickerCfg = !!localStorage.getItem('tickerBarConfig');
    setTimeout(function() { _tickerRebuild(); }, _hasLocalTickerCfg ? 500 : 1500);

    // One-time: push existing localStorage config to server if it hasn't been synced yet
    if (_hasLocalTickerCfg && window.PreferenceSync) {
        try { PreferenceSync.syncPreference('ticker_prefs', JSON.parse(localStorage.getItem('tickerBarConfig'))); } catch(e) {}
    }

    // Periodic refreshes
    if (tickerRefreshInterval) clearInterval(tickerRefreshInterval);
    tickerRefreshInterval = setInterval(function() {
        _tickerFetchSentiment();
        _tickerFetchAlerts();
        _tickerFetchNews();
        _tickerRebuild(); // picks up position changes
    }, TICKER_REFRESH_MS);

    // Faster refresh: prices every 2s, alerts every 60s, positions every 10s
    if (_tickerPriceIntervalId) clearInterval(_tickerPriceIntervalId);
    _tickerPriceIntervalId = setInterval(function() {
        if (document.hidden) return;
        if (typeof _tickerSchedulePriceRefresh === 'function') _tickerSchedulePriceRefresh();
    }, 2000);

    if (_tickerAlertIntervalId) clearInterval(_tickerAlertIntervalId);
    _tickerAlertIntervalId = setInterval(function() {
        _tickerFetchAlerts();
    }, TICKER_ALERT_REFRESH_MS);

    // Pause on hover + drag to scroll
    var container = document.getElementById('ticker-container');
    if (container) {
        container.addEventListener('mouseenter', function() { _tickerPaused = true; });
        container.addEventListener('mouseleave', function() {
            if (!_tickerDragging) _tickerPaused = false;
        });

        // Drag to scrub
        var dragStartX = 0;
        var dragStartOffset = 0;

        container.addEventListener('mousedown', function(e) {
            if (e.button !== 0) return;
            _tickerDragging = true;
            _tickerPaused = true;
            dragStartX = e.clientX;
            dragStartOffset = _tickerOffset;
            container.style.cursor = 'grabbing';
            e.preventDefault();
        });

        document.addEventListener('mousemove', function(e) {
            if (!_tickerDragging) return;
            var dx = e.clientX - dragStartX;
            _tickerOffset = dragStartOffset + dx;
            var track = document.getElementById('ticker-track');
            if (track) track.style.transform = 'translateX(' + _tickerOffset + 'px)';
        });

        document.addEventListener('mouseup', function() {
            if (!_tickerDragging) return;
            _tickerDragging = false;
            container.style.cursor = '';
            // Resume auto-scroll after a brief pause
            setTimeout(function() {
                if (!container.matches(':hover')) _tickerPaused = false;
            }, 2000);
        });

        // Touch drag
        container.addEventListener('touchstart', function(e) {
            _tickerDragging = true;
            _tickerPaused = true;
            dragStartX = e.touches[0].clientX;
            dragStartOffset = _tickerOffset;
        }, { passive: true });

        container.addEventListener('touchmove', function(e) {
            if (!_tickerDragging) return;
            var dx = e.touches[0].clientX - dragStartX;
            _tickerOffset = dragStartOffset + dx;
            var track = document.getElementById('ticker-track');
            if (track) track.style.transform = 'translateX(' + _tickerOffset + 'px)';
        }, { passive: true });

        container.addEventListener('touchend', function() {
            _tickerDragging = false;
            setTimeout(function() { _tickerPaused = false; }, 2000);
        });

        container.style.cursor = 'grab';
    }

    // Config button
    var btn = document.getElementById('ticker-config-btn');
    if (btn) {
        btn.addEventListener('click', function(e) { e.stopPropagation(); _tickerOpenConfig(); });
    }

    // News item click handler (event delegation)
    if (container) {
        container.addEventListener('click', function(e) {
            if (_tickerDragging) return;
            var item = e.target.closest('[data-module="news"]');
            if (!item) return;
            var newsId = item.getAttribute('data-news-id');
            if (!newsId || !_tickerNewsData) return;
            var newsItem = null;
            for (var i = 0; i < _tickerNewsData.length; i++) {
                if (_tickerNewsData[i].id === newsId) { newsItem = _tickerNewsData[i]; break; }
            }
            if (newsItem) _tickerShowNewsPreview(newsItem);
        });
    }

    // Rebuild on window resize (static vs scroll may change)
    var resizeTimer;
    window.addEventListener('resize', function() {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(_tickerRebuild, 300);
    });
}
