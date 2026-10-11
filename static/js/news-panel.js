/* News Panel — RSS feed reader in the right-side panel.
 * Deps: split-layout.js (setViewMode), prefs-sync.js (PreferenceSync),
 *       shared-utils.js (showToast), csrf-utils.js (getCSRFToken) */

var _npData = [];
var _npActiveArticle = null;
var _npSettingsOpen = false;
var _npCountdownTimer = null;
var _npLastFetch = 0;
var _npReadIds = {};  // id -> true
var _NP_MAX_READ = 200;
var _NP_REFRESH_OPTIONS = [
    { label: '1 min', ms: 60000 },
    { label: '2 min', ms: 120000 },
    { label: '5 min', ms: 300000 },
    { label: '10 min', ms: 600000 }
];

var _NP_FEED_NAMES = {
    cnbc_top: 'CNBC', dlnews: 'DL News', coindesk: 'CoinDesk',
    cointelegraph: 'CoinTelegraph', theblock: 'The Block', decrypt: 'Decrypt',
    marketwatch: 'MarketWatch', seekingalpha: 'Seeking Alpha',
    fed: 'Federal Reserve', fxstreet: 'FXStreet'
};
var _NP_ALL_FEEDS = ['cnbc_top','dlnews','coindesk','cointelegraph','theblock','decrypt','marketwatch','seekingalpha','fed','fxstreet'];
var _NP_FONT_SIZES = [0.72, 0.8, 0.9, 1.0, 1.1];
var _NP_DEFAULT_PREFS = {
    feeds: ['coindesk', 'cointelegraph', 'decrypt'],
    customFeeds: [],
    groupMode: 'chrono',
    fontIdx: 1,
    refreshMs: 120000  // 2 min default
};

// ── Preferences ──

function _npGetPrefs() {
    try {
        var saved = JSON.parse(localStorage.getItem('newsPanelPrefs'));
        if (saved) {
            var p = JSON.parse(JSON.stringify(_NP_DEFAULT_PREFS));
            if (saved.feeds && Array.isArray(saved.feeds)) p.feeds = saved.feeds;
            if (saved.customFeeds && Array.isArray(saved.customFeeds)) p.customFeeds = saved.customFeeds;
            if (saved.groupMode) p.groupMode = saved.groupMode;
            if (saved.fontIdx !== undefined) p.fontIdx = Math.max(0, Math.min(_NP_FONT_SIZES.length - 1, saved.fontIdx));
            if (saved.refreshMs) p.refreshMs = saved.refreshMs;
            return p;
        }
    } catch(e) {}
    return JSON.parse(JSON.stringify(_NP_DEFAULT_PREFS));
}

function _npSavePrefs(prefs) {
    try { localStorage.setItem('newsPanelPrefs', JSON.stringify(prefs)); } catch(e) {}
    if (window.PreferenceSync) PreferenceSync.syncPreference('news_panel_prefs', prefs);
}

// ── Read state ──

function _npLoadReadIds() {
    try {
        var arr = JSON.parse(localStorage.getItem('newsReadIds'));
        if (Array.isArray(arr)) {
            _npReadIds = {};
            arr.forEach(function(id) { _npReadIds[id] = true; });
        }
    } catch(e) {}
}

function _npMarkRead(id) {
    _npReadIds[id] = true;
    var keys = Object.keys(_npReadIds);
    if (keys.length > _NP_MAX_READ) {
        var trimmed = keys.slice(keys.length - _NP_MAX_READ);
        _npReadIds = {};
        trimmed.forEach(function(k) { _npReadIds[k] = true; });
    }
    try { localStorage.setItem('newsReadIds', JSON.stringify(Object.keys(_npReadIds))); } catch(e) {}
    // Update DOM directly
    var el = document.querySelector('.np-article[data-article-id="' + id + '"]');
    if (el) el.classList.add('np-article-read');
}

// ── Utilities ──

function _npTimeAgo(iso) {
    if (!iso) return '';
    try {
        var ms = Date.now() - new Date(iso).getTime();
        if (ms < 0) return 'now';
        var mins = Math.floor(ms / 60000);
        if (mins < 1) return 'now';
        if (mins < 60) return mins + 'm';
        var hrs = Math.floor(mins / 60);
        if (hrs < 24) return hrs + 'h';
        var days = Math.floor(hrs / 24);
        if (days < 7) return days + 'd';
        return Math.floor(days / 7) + 'w';
    } catch(e) { return ''; }
}

function _npApplyFont() {
    var prefs = _npGetPrefs();
    var sz = _NP_FONT_SIZES[prefs.fontIdx] || 0.8;
    var content = document.getElementById('news-panel-content');
    if (content) content.style.fontSize = sz + 'rem';
}

// ── Initialization ──

function initNewsPanel() {
    _npLoadReadIds();

    // Font buttons
    var fontDown = document.getElementById('news-font-down');
    var fontUp = document.getElementById('news-font-up');
    if (fontDown) fontDown.addEventListener('click', function() {
        var p = _npGetPrefs();
        if (p.fontIdx > 0) { p.fontIdx--; _npSavePrefs(p); _npApplyFont(); }
    });
    if (fontUp) fontUp.addEventListener('click', function() {
        var p = _npGetPrefs();
        if (p.fontIdx < _NP_FONT_SIZES.length - 1) { p.fontIdx++; _npSavePrefs(p); _npApplyFont(); }
    });

    // Gear button
    var gear = document.getElementById('news-panel-gear');
    if (gear) gear.addEventListener('click', function(ev) {
        ev.stopPropagation();
        if (_npSettingsOpen) _npHideSettings(); else _npShowSettings();
    });

    // Grouping toggle (inline in header)
    var groupToggle = document.getElementById('np-group-toggle');
    if (groupToggle) groupToggle.addEventListener('click', function(ev) {
        var btn = ev.target.closest('.np-group-btn');
        if (!btn) return;
        var mode = btn.getAttribute('data-group');
        groupToggle.querySelectorAll('.np-group-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        var p = _npGetPrefs();
        p.groupMode = mode;
        _npSavePrefs(p);
        _npRenderList();
    });

    // Close settings on outside click
    document.addEventListener('click', function(ev) {
        if (_npSettingsOpen && !ev.target.closest('.news-panel-settings') && !ev.target.closest('.news-panel-gear-btn')) {
            _npHideSettings();
        }
    });

    // Pause/resume refresh on view mode change + refresh on return
    document.addEventListener('carrac:viewmode', function(ev) {
        if (ev.detail && ev.detail.mode === 'news') {
            _npStartRefreshTimer();
            refreshNewsPanel(); // always refresh when switching back
        } else {
            _npStopTimers();
        }
    });

    // Start auto-refresh + countdown
    _npStartRefreshTimer();

    _npApplyFont();
}

function _npStopTimers() {
    if (_npCountdownTimer) { clearInterval(_npCountdownTimer); _npCountdownTimer = null; }
}

function _npStartRefreshTimer() {
    _npStopTimers();
    var el = document.getElementById('np-countdown');
    var refreshMs = _npGetPrefs().refreshMs || 120000;

    function tick() {
        if (document.hidden) return;
        var elapsed = Date.now() - _npLastFetch;
        var remaining = Math.max(0, Math.ceil((refreshMs - elapsed) / 1000));
        var m = Math.floor(remaining / 60);
        var s = remaining % 60;
        if (el) el.textContent = m + ':' + (s < 10 ? '0' : '') + s;
        if (remaining <= 0) refreshNewsPanel();
    }
    tick();
    _npCountdownTimer = setInterval(tick, 1000);
}

// ── Fetch & Render ──

function refreshNewsPanel(force) {
    var prefs = _npGetPrefs();
    var refreshMs = prefs.refreshMs || 120000;
    // Skip if recently fetched (unless forced)
    if (!force && _npData.length > 0 && (Date.now() - _npLastFetch) < refreshMs) {
        _npRenderList();
        return;
    }
    var feeds = prefs.feeds.filter(function(f) { return _NP_ALL_FEEDS.indexOf(f) !== -1; });
    var customUrls = (prefs.customFeeds || []).filter(function(f) { return f.enabled; }).map(function(f) { return f.url; });

    if (feeds.length === 0 && customUrls.length === 0) {
        _npData = [];
        _npRenderList();
        return;
    }

    var url = '/api/market-news?feeds=' + encodeURIComponent(feeds.join(','));
    if (customUrls.length > 0) {
        url += '&custom=' + encodeURIComponent(customUrls.join(','));
    }

    var content = document.getElementById('news-panel-content');
    if (content && _npData.length === 0) {
        content.innerHTML = '<div class="np-loading">Loading news...</div>';
    }

    fetch(url, { credentials: 'same-origin' })
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(data) {
        if (!data || !data.items) return;
        _npData = data.items;
        _npLastFetch = Date.now();
        _npRenderList();
    })
    .catch(function() {
        if (_npData.length === 0) {
            var el = document.getElementById('news-panel-content');
            if (el) el.innerHTML = '<div class="np-empty"><div class="np-empty-title">Failed to load news</div><div class="np-empty-desc">Check your connection and try again.</div></div>';
        }
    });
}

function _npRenderList() {
    var content = document.getElementById('news-panel-content');
    if (!content) return;

    // Update count
    var countEl = document.getElementById('news-panel-count');
    if (countEl) countEl.textContent = _npData.length > 0 ? _npData.length + ' articles' : '';

    if (_npData.length === 0) {
        _npActiveArticle = null;
        var prefs = _npGetPrefs();
        if (prefs.feeds.length === 0 && (!prefs.customFeeds || prefs.customFeeds.length === 0)) {
            content.innerHTML = '<div class="np-empty"><div class="np-empty-title">No feeds selected</div><div class="np-empty-desc">Click the gear icon to choose your news sources.</div></div>';
        } else {
            content.innerHTML = '<div class="np-empty"><div class="np-empty-title">No articles yet</div><div class="np-empty-desc">Articles will appear here shortly.</div></div>';
        }
        return;
    }

    var prefs = _npGetPrefs();
    var html = '';

    if (prefs.groupMode === 'source') {
        var groups = {};
        var order = [];
        _npData.forEach(function(item) {
            var src = item.source || 'Unknown';
            if (!groups[src]) { groups[src] = []; order.push(src); }
            groups[src].push(item);
        });
        order.forEach(function(src) {
            html += '<div class="np-source-group">';
            html += '<div class="np-source-group-header">' + escapeHtml(src) + ' <span style="opacity:0.5;font-weight:400">' + groups[src].length + '</span></div>';
            groups[src].forEach(function(item) { html += _npRenderCard(item); });
            html += '</div>';
        });
    } else {
        _npData.forEach(function(item) { html += _npRenderCard(item); });
    }

    content.innerHTML = html;
    _npBindCardEvents(content);
    _npApplyFont();
}

function _npRenderCard(item) {
    var read = _npReadIds[item.id] ? ' np-article-read' : '';
    var expanded = (_npActiveArticle && _npActiveArticle.id === item.id) ? ' np-article-expanded' : '';
    var ago = _npTimeAgo(item.published);

    var linkHtml = '';
    if (item.link && (item.link.indexOf('https://') === 0 || item.link.indexOf('http://') === 0)) {
        linkHtml = '<a href="' + escapeHtml(item.link) + '" target="_blank" rel="noopener noreferrer" class="np-article-body-link">Read full article &rarr;</a>';
    }
    var summary = item.summary || '';

    return '<div class="np-article' + read + expanded + '" data-article-id="' + escapeHtml(item.id) + '">' +
        '<div class="np-article-row">' +
            '<span class="np-article-source">' + escapeHtml(item.source || 'News') + '</span>' +
            '<span class="np-article-title">' + escapeHtml(item.title || 'Untitled') + '</span>' +
            '<span class="np-article-meta">' + escapeHtml(ago) + '</span>' +
        '</div>' +
        '<div class="np-article-body">' +
            (summary ? '<div class="np-article-body-summary">' + escapeHtml(summary) + '</div>' : '') +
            '<div class="np-article-body-actions">' +
                linkHtml +
                '<button class="np-ask-ai-btn">\u26A1 Ask AI</button>' +
            '</div>' +
        '</div>' +
    '</div>';
}

function _npBindCardEvents(container) {
    container.querySelectorAll('.np-article').forEach(function(el) {
        el.addEventListener('click', function() {
            var id = el.getAttribute('data-article-id');
            var item = _npData.find(function(d) { return d.id === id; });
            if (!item) return;
            _npToggleArticle(el, item);
        });
    });
    // "Read full article" link — keep the card's row click (expand/collapse) from firing
    container.querySelectorAll('.np-article-body-link').forEach(function(link) {
        link.addEventListener('click', function(ev) { ev.stopPropagation(); });
    });
    // Ask AI button — same stopPropagation guard, then bridge to the AI panel
    container.querySelectorAll('.np-ask-ai-btn').forEach(function(btn) {
        btn.addEventListener('click', function(ev) {
            ev.stopPropagation();
            _npAskAI();
        });
    });
}

// ── Inline accordion ──

function _npToggleArticle(el, item) {
    var wasExpanded = el.classList.contains('np-article-expanded');

    // Collapse any currently expanded card
    var prev = document.querySelector('.np-article-expanded');
    if (prev) prev.classList.remove('np-article-expanded');

    if (wasExpanded) {
        // Toggle off
        _npActiveArticle = null;
    } else {
        // Expand this card
        el.classList.add('np-article-expanded');
        _npActiveArticle = item;
        _npMarkRead(item.id);
        // Scroll into view after animation
        setTimeout(function() {
            el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }, 50);
    }
}

// ── Ask AI bridge ──

function _npAskAI() {
    if (!_npActiveArticle) return;
    // Open AI panel if closed
    if (typeof setAIPanelState === 'function' && typeof _aiPanelState !== 'undefined' && _aiPanelState === 'fab') {
        setAIPanelState('small');
    }
    // Set copilot focus to news
    if (typeof _copilotLastArea !== 'undefined') _copilotLastArea = 'news';
    // Refresh AI panel state for news context
    if (typeof _updateQuickActions === 'function') _updateQuickActions();
    if (typeof _updateCopilotTitle === 'function') _updateCopilotTitle();
    if (typeof _renderGuidedEmptyState === 'function') _renderGuidedEmptyState();
}

function _npGetActiveArticleContext() {
    if (!_npActiveArticle) return null;
    return {
        title: _npActiveArticle.title || '',
        summary: _npActiveArticle.summary || '',
        source: _npActiveArticle.source || '',
        published: _npActiveArticle.published || ''
    };
}

// ── Settings Popover ──

function _npShowSettings() {
    _npSettingsOpen = true;
    var prefs = _npGetPrefs();
    var container = document.getElementById('panel-news-view');
    if (!container) return;

    // Remove existing
    var existing = container.querySelector('.news-panel-settings');
    if (existing) existing.remove();

    var div = document.createElement('div');
    div.className = 'news-panel-settings';
    div.id = 'news-panel-settings-popover';

    var isPremium = window._isPremium;
    var html = '<div class="nps-header">News Feed Settings</div>';

    // Refresh rate
    html += '<div class="nps-section">Refresh Rate</div>';
    html += '<div class="nps-refresh-btns">';
    _NP_REFRESH_OPTIONS.forEach(function(opt) {
        var active = prefs.refreshMs === opt.ms ? ' nps-refresh-active' : '';
        html += '<button class="nps-refresh-btn' + active + '" data-refresh="' + opt.ms + '">' + opt.label + '</button>';
    });
    html += '</div>';

    // Built-in feeds
    html += '<div class="nps-section">Feeds</div>';
    _NP_ALL_FEEDS.forEach(function(fid) {
        var checked = prefs.feeds.indexOf(fid) !== -1 ? ' checked' : '';
        html += '<label class="nps-row"><input type="checkbox" data-feed="' + fid + '"' + checked + '><span>' + escapeHtml(_NP_FEED_NAMES[fid] || fid) + '</span></label>';
    });

    // Custom RSS (premium only)
    if (isPremium) {
        html += '<div class="nps-section">Custom RSS</div>';
        html += '<input type="text" class="nps-custom-input" id="nps-custom-url" placeholder="https://example.com/rss">';
        html += '<div style="display:flex;gap:4px;align-items:center;margin-bottom:6px;">';
        html += '<input type="text" class="nps-custom-input" id="nps-custom-label" placeholder="Feed name" style="flex:1;margin-bottom:0">';
        html += '<button class="nps-test-btn" id="nps-test-btn">Test</button>';
        html += '<button class="nps-test-btn" id="nps-add-btn" disabled>+ Add</button>';
        html += '</div>';
        html += '<div class="nps-test-result" id="nps-test-result"></div>';

        // Existing custom feeds
        (prefs.customFeeds || []).forEach(function(f, idx) {
            html += '<div class="nps-custom-item">';
            html += '<label class="nps-row" style="flex:1"><input type="checkbox" data-customfeed="' + idx + '"' + (f.enabled ? ' checked' : '') + '><span>' + escapeHtml(f.label || f.url) + '</span></label>';
            html += '<button class="nps-custom-remove" data-removefeed="' + idx + '" title="Remove">&times;</button>';
            html += '</div>';
        });
    }

    div.innerHTML = html;
    div.addEventListener('click', function(ev) { ev.stopPropagation(); });
    container.appendChild(div);

    // Bind refresh rate buttons
    div.querySelectorAll('.nps-refresh-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            div.querySelectorAll('.nps-refresh-btn').forEach(function(b) { b.classList.remove('nps-refresh-active'); });
            btn.classList.add('nps-refresh-active');
            var p = _npGetPrefs();
            p.refreshMs = parseInt(btn.getAttribute('data-refresh'));
            _npSavePrefs(p);
            _npStartRefreshTimer();
        });
    });

    // Bind feed toggles
    div.querySelectorAll('input[data-feed]').forEach(function(cb) {
        cb.addEventListener('change', function() {
            var p = _npGetPrefs();
            var fid = cb.getAttribute('data-feed');
            if (cb.checked) {
                if (p.feeds.indexOf(fid) === -1) p.feeds.push(fid);
            } else {
                p.feeds = p.feeds.filter(function(f) { return f !== fid; });
            }
            _npSavePrefs(p);
            _npLastFetch = 0; // force refresh
            refreshNewsPanel(true);
        });
    });

    // Custom feed enable/disable
    div.querySelectorAll('input[data-customfeed]').forEach(function(cb) {
        cb.addEventListener('change', function() {
            var p = _npGetPrefs();
            var idx = parseInt(cb.getAttribute('data-customfeed'));
            if (p.customFeeds[idx]) {
                p.customFeeds[idx].enabled = cb.checked;
                _npSavePrefs(p);
                _npLastFetch = 0;
                refreshNewsPanel(true);
            }
        });
    });

    // Custom feed remove
    div.querySelectorAll('.nps-custom-remove').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var p = _npGetPrefs();
            var idx = parseInt(btn.getAttribute('data-removefeed'));
            p.customFeeds.splice(idx, 1);
            _npSavePrefs(p);
            _npHideSettings();
            _npShowSettings();
            _npLastFetch = 0;
            refreshNewsPanel(true);
        });
    });

    // Test + Add buttons
    if (isPremium) {
        var testBtn = div.querySelector('#nps-test-btn');
        var addBtn = div.querySelector('#nps-add-btn');
        var urlInput = div.querySelector('#nps-custom-url');
        var labelInput = div.querySelector('#nps-custom-label');
        var resultEl = div.querySelector('#nps-test-result');

        if (testBtn) testBtn.addEventListener('click', function() {
            var url = urlInput ? urlInput.value.trim() : '';
            if (!url || url.indexOf('https://') !== 0) {
                if (resultEl) { resultEl.textContent = 'URL must start with https://'; resultEl.style.color = '#f85149'; }
                return;
            }
            if (resultEl) { resultEl.textContent = 'Testing...'; resultEl.style.color = '#8b949e'; }
            testBtn.disabled = true;

            var csrf = document.querySelector('meta[name="csrf-token"]');
            fetch('/api/market-news/test', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrf ? csrf.content : ''
                },
                credentials: 'same-origin',
                body: JSON.stringify({ url: url })
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                testBtn.disabled = false;
                if (data.status === 'ok') {
                    if (resultEl) { resultEl.textContent = 'Found ' + data.item_count + ' items from "' + (data.title || 'feed') + '"'; resultEl.style.color = '#3fb950'; }
                    if (addBtn) addBtn.disabled = false;
                    if (labelInput && !labelInput.value && data.title) labelInput.value = data.title;
                } else {
                    if (resultEl) { resultEl.textContent = data.error || 'Invalid feed'; resultEl.style.color = '#f85149'; }
                    if (addBtn) addBtn.disabled = true;
                }
            })
            .catch(function() {
                testBtn.disabled = false;
                if (resultEl) { resultEl.textContent = 'Test failed'; resultEl.style.color = '#f85149'; }
            });
        });

        if (addBtn) addBtn.addEventListener('click', function() {
            var url = urlInput ? urlInput.value.trim() : '';
            var label = labelInput ? labelInput.value.trim() : '';
            if (!url) return;
            var p = _npGetPrefs();
            // Deduplicate
            var dup = p.customFeeds.some(function(f) { return f.url === url; });
            if (dup) {
                if (resultEl) { resultEl.textContent = 'Feed already added'; resultEl.style.color = '#f59e0b'; }
                return;
            }
            if (p.customFeeds.length >= 5) {
                if (resultEl) { resultEl.textContent = 'Max 5 custom feeds'; resultEl.style.color = '#f85149'; }
                return;
            }
            p.customFeeds.push({ url: url, label: label || url, enabled: true });
            _npSavePrefs(p);
            _npHideSettings();
            _npShowSettings();
            _npLastFetch = 0;
            refreshNewsPanel(true);
        });
    }
}

function _npHideSettings() {
    _npSettingsOpen = false;
    var el = document.getElementById('news-panel-settings-popover');
    if (el) el.remove();
}
