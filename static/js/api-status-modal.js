// API Status Modal
function openApiStatusModal() {
    var modal = document.getElementById('api-status-modal');
    if (!modal) return;
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
    if (window.a11y) a11y.trapFocus(modal.querySelector('[role="dialog"]'));
    refreshApiStatus();
}

function closeApiStatusModal() {
    var modal = document.getElementById('api-status-modal');
    if (!modal) return;
    if (window.a11y) a11y.releaseFocus(modal.querySelector('[role="dialog"]'));
    modal.classList.remove('active');
    document.body.style.overflow = '';
}

function refreshApiStatus() {
    var contentEl = document.getElementById('api-status-content');
    var usageEl = document.getElementById('api-usage-content');
    var timestampEl = document.getElementById('api-status-timestamp');
    if (contentEl) contentEl.innerHTML = '<div class="api-loading">Checking API connections...</div>';
    if (usageEl) usageEl.innerHTML = '<div class="api-loading">Loading usage stats...</div>';

    // Fetch both API status and usage in parallel
    Promise.all([
        fetch('/api/status').then(function(r) { return r.json(); }),
        fetch('/api/providers/usage').then(function(r) { return r.json(); }).catch(function() { return null; })
    ]).then(function(results) {
        var data = results[0];
        var usageData = results[1];

        // Render usage stats if available
        if (usageEl && usageData && usageData.providers) {
            renderUsageStats(usageEl, usageData.providers);
        }

        // Continue with API status rendering
        if (contentEl) renderApiStatus(contentEl, data);
        if (timestampEl) timestampEl.textContent = 'Checked: ' + new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    }).catch(function(err) {
        contentEl.innerHTML = '<div class="api-loading" style="color: #f85149;">Failed to check API status: ' + escapeHtml(err.message) + '</div>';
    });
}

function renderUsageStats(container, providers) {
    var html = '';
    // Sort providers by description for consistent display
    var providerKeys = Object.keys(providers).sort(function(a, b) {
        return (providers[a].description || a).localeCompare(providers[b].description || b);
    });

    providerKeys.forEach(function(key) {
        var p = providers[key];
        var statusClass = 'status-' + p.status;
        var pctWidth = Math.min(100, p.pct_used || 0);

        html += '<div class="api-usage-item">';
        html += '<div class="api-usage-header">';
        html += '<span class="api-usage-name">' + escapeHtml(p.description || key) + '</span>';
        html += '<span class="api-usage-stats">' + escapeHtml(String(p.current)) + ' / ' + escapeHtml(String(p.limit)) + ' (' + escapeHtml(String(p.pct_used)) + '%)</span>';
        html += '</div>';
        html += '<div class="api-usage-bar">';
        html += '<div class="api-usage-fill ' + statusClass + '" style="width: ' + pctWidth + '%"></div>';
        html += '</div>';
        if (p.reset_human && p.reset_human !== 'now') {
            html += '<div class="api-usage-window">Resets in: ' + escapeHtml(p.reset_human) + ' | Window: ' + escapeHtml(p.window_human) + '</div>';
        } else {
            html += '<div class="api-usage-window">Window: ' + escapeHtml(p.window_human) + '</div>';
        }
        html += '</div>';
    });

    container.innerHTML = html || '<div class="api-loading">No usage data available</div>';
}

function renderApiStatus(contentEl, data) {
    var html = '';
    var apis = data.apis || {};
    var summary = data.summary || {};

    // Show summary at top
    html += '<div class="api-summary">';
    html += '<span class="api-summary-item primary"><strong>Primary:</strong> ' + escapeHtml(summary.primary || '?') + '</span>';
    html += '<span class="api-summary-item fallback"><strong>Fallback:</strong> ' + escapeHtml(summary.fallback || '?') + '</span>';
    html += '</div>';

    // Group APIs by category
    var categories = {
        'TradFi': { name: 'Traditional Finance', order: ['stooq'] },
        'Prices': { name: 'Realtime Prices', order: ['crypto_market_data', 'coingecko', 'binance'] },
        'Sentiment': { name: 'Sentiment', order: ['feargreed'] },
        'Metrics': { name: 'Crypto Metrics', order: ['crypto_metrics'] }
    };

    Object.keys(categories).forEach(function(catKey) {
        var cat = categories[catKey];
        var categoryApis = cat.order.filter(function(key) { return apis[key]; });
        if (categoryApis.length === 0) return;

        html += '<div class="api-category">';
        html += '<div class="api-category-header">' + cat.name + '</div>';

        categoryApis.forEach(function(key) {
            var api = apis[key];
            if (!api) return;

            var statusClass = 'status-' + api.status;
            var badgeClass = api.status;
            var badgeText = api.status === 'ok' ? 'OK' : (api.status === 'ratelimit' ? 'LIMIT' : (api.status === 'stale' ? 'STALE' : 'ERR'));
            var tierLabel = api.tier === 'primary' ? '★' : (api.tier === 'fallback' ? '↓' : (api.tier === 'overlay' ? '⊕' : '◇'));

            html += '<div class="api-status-item ' + statusClass + '">';
            html += '<div class="api-status-info">';
            html += '<div class="api-status-name">';
            html += '<span class="api-tier-icon" title="' + escapeHtml(api.tier) + '">' + tierLabel + '</span> ';
            html += escapeHtml(api.name);
            html += '</div>';
            html += '<div class="api-status-desc">' + escapeHtml(api.desc) + '</div>';
            html += '<div class="api-status-rate">' + escapeHtml(api.rate_limit || '') + '</div>';
            if (api.error) {
                html += '<div class="api-status-error">' + escapeHtml(api.error) + '</div>';
            }
            html += '</div>';
            html += '<span class="api-status-badge ' + badgeClass + '">' + badgeText + '</span>';
            html += '</div>';
        });

        html += '</div>';
    });

    contentEl.innerHTML = html;
}

// Close modal on Escape key (skip if chart sub-panels are handling ESC)
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        if (document.getElementById('text-annotation-dialog')) return;
        if (document.getElementById('shortcuts-modal')) return;
        var sp = document.getElementById('asset-search-popup');
        if (sp && sp.classList.contains('visible')) return;
        if (typeof drawerOpen !== 'undefined' && drawerOpen) return;
        var as = document.getElementById('asset-switcher-menu');
        if (as && as.classList.contains('visible')) return;
        var helpModal = document.getElementById('help-modal');
        if (helpModal && helpModal.classList.contains('active')) closeHelpModal();
        var apiModal = document.getElementById('api-status-modal');
        if (apiModal && apiModal.classList.contains('active')) closeApiStatusModal();
        var premiumModal = document.getElementById('premium-modal');
        if (premiumModal && premiumModal.classList.contains('active')) closePremiumModal();
        hideChartTooltip();
    }
});
