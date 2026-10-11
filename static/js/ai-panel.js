/**
 * AI Panel for Carrac Chart Page — AI chat with presets.
 * Toggle: Q key switches to AI view.
 * Chat: Quick action buttons (Analyze, Validate, Challenge, Checklist, Confluence, Patterns)
 *        + SSE streaming AI chat.
 * Journal: Separate vertical panel (initJournalPanel).
 */

var aiPanelStreaming = false;
var aiPanelBalance = 0;
var _aiPanelInitDone = false;
var _aiDocClickAttached = false;
var _journalLoaded = false;
var _patternMatchData = null;
var _patternPanelShrunk = false;
var _patternGhostRegistered = false;
var _aiConversationId = (function() {
    try { var v = localStorage.getItem('aiConversationId'); return v ? parseInt(v) : null; } catch(e) { return null; }
})();

// Global font size state (accessible from initAIPanel button handlers)
var _aiFontSizes = [0.65, 0.72, 0.8, 0.9, 1.0];
var _aiFontIdx = (function() {
    var saved = parseFloat(localStorage.getItem('aiFontSize') || '0.72');
    var idx = _aiFontSizes.indexOf(saved);
    return idx !== -1 ? idx : 1;
})();

function _aiApplyFont() {
    var sz = _aiFontSizes[_aiFontIdx];
    localStorage.setItem('aiFontSize', String(sz));
    var c = document.getElementById('ai-panel-chat');
    if (c) c.style.fontSize = sz + 'rem';
    // Auto-resize small chat window based on font size
    var panel = document.getElementById('ai-float');
    if (panel && _aiPanelState === 'small') {
        var baseW = 360, baseH = 520;
        var scale = sz / 0.72; // relative to default font
        panel.style.width = Math.round(Math.max(baseW, baseW * scale)) + 'px';
        panel.style.height = Math.round(Math.max(baseH, baseH * scale)) + 'px';
    }
}

// ===== Three-state panel: 'fab' | 'small' | 'column' =====
var _aiPanelState = 'fab';

// localStorage migration: convexity_ → carrac_ (one-time per key)
(function() {
    ['carrac_account_size'].forEach(function(old) {
        try {
            var v = localStorage.getItem(old);
            var newKey = old.replace(/^convexity_/, 'carrac_');
            if (v !== null && localStorage.getItem(newKey) === null) {
                localStorage.setItem(newKey, v);
            }
            localStorage.removeItem(old);
        } catch(e) {}
    });
})();

// Apply saved font size + bind FAB on DOM ready.
// When lazy-loaded, DOMContentLoaded has already fired — run immediately.
(function aiPanelDOMInit() {
    function init() {
    _aiApplyFont();

    // FAB toggle button → opens small chat
    var fab = document.getElementById('ai-fab');
    if (fab) fab.addEventListener('click', function() { setAIPanelState('small'); });

    // Window control buttons (Windows-style)
    var winMinBtn = document.getElementById('ai-win-minimize');
    var winDockBtn = document.getElementById('ai-win-dock');
    var winCloseBtn = document.getElementById('ai-win-close');
    if (winMinBtn) winMinBtn.addEventListener('click', function() { setAIPanelState('fab'); });
    if (winDockBtn) winDockBtn.addEventListener('click', function() {
        if (_aiPanelState === 'small') setAIPanelState('column');
        else if (_aiPanelState === 'column') setAIPanelState('small');
    });
    if (winCloseBtn) winCloseBtn.addEventListener('click', function() {
        setAIPanelState('fab');
        try { localStorage.removeItem('aiPanelLastOpen'); } catch(e) {}
    });

    // Overflow menu toggle
    var overflowBtn = document.getElementById('ai-overflow-btn');
    var overflowMenu = document.getElementById('ai-overflow-menu');
    if (overflowBtn && overflowMenu) {
        overflowBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var vis = overflowMenu.style.display !== 'none';
            overflowMenu.style.display = vis ? 'none' : 'block';
            if (!vis) {
                setTimeout(function() {
                    document.addEventListener('click', function _closeOvf() {
                        overflowMenu.style.display = 'none';
                        document.removeEventListener('click', _closeOvf);
                    }, { once: true });
                }, 0);
            }
        });
    }

    // Font size buttons (inside overflow menu)
    var fontDown = document.getElementById('ai-font-down');
    var fontUp = document.getElementById('ai-font-up');
    if (fontDown) fontDown.addEventListener('click', function() {
        if (_aiFontIdx > 0) { _aiFontIdx--; _aiApplyFont(); }
    });
    if (fontUp) fontUp.addEventListener('click', function() {
        if (_aiFontIdx < _aiFontSizes.length - 1) { _aiFontIdx++; _aiApplyFont(); }
    });

    // Actions dropdown toggle
    var actionsBtn = document.getElementById('ai-actions-btn');
    var actionsMenu = document.getElementById('ai-quick-actions');
    if (actionsBtn && actionsMenu) {
        actionsBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var vis = actionsMenu.classList.contains('visible');
            actionsMenu.classList.toggle('visible', !vis);
            if (!vis) {
                _updateQuickActions();
                setTimeout(function() {
                    document.addEventListener('click', function _closeAct() {
                        actionsMenu.classList.remove('visible');
                        document.removeEventListener('click', _closeAct);
                    }, { once: true });
                }, 0);
            }
        });
    }

    // History panel toggle
    var historyBtn = document.getElementById('ai-history-btn');
    var historyBack = document.getElementById('ai-history-back');
    if (historyBtn) historyBtn.addEventListener('click', function() { _toggleHistoryPanel(true); });
    if (historyBack) historyBack.addEventListener('click', function() { _toggleHistoryPanel(false); });

    // Global ESC — cascade: column→small, small→fab
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && _aiPanelState !== 'fab') {
            // Don't close if user is typing in textarea — just blur
            if (document.activeElement && document.activeElement.id === 'ai-panel-textarea') return;
            e.preventDefault();
            if (_aiPanelState === 'column') setAIPanelState('small');
            else setAIPanelState('fab');
        }
    });

    // Migrate: if localStorage has panelViewMode='ai', reset to 'consensio'
    try {
        if (localStorage.getItem('panelViewMode') === 'ai') {
            localStorage.setItem('panelViewMode', 'consensio');
        }
    } catch(e) {}

    // Reset cached score on asset change + update badge + chip
    document.addEventListener('carrac:assetchange', function() {
        _aiLastScore = null;
        _aiScoreFetching = false;
        _updateFabBadge();
        if (_aiPanelState !== 'fab') {
            _updateQuickActions();
            _updateContextBar();
            _updateContextChip();
        }
    });

    // Show onboarding tooltip + initial badge state
    _updateFabBadge();
    _showOnboardingTooltip();

    // Restore panel state from last session
    try {
        var savedState = localStorage.getItem('aiPanelLastOpen');
        if (savedState === 'column' || savedState === 'small') {
            setTimeout(function() { setAIPanelState(savedState); }, 300);
        }
    } catch(e) {}
    }  // end init()
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();  // DOM already ready — fire synchronously (lazy-load path)
    }
}());

var VIEW_MODE_ORDER = ['minimal', 'consensio', 'full', 'news', 'report', 'plays', 'journal', 'portfolio'];

// Page-specific quick actions
var PAGE_ACTIONS = {
    chart: [
        {label: 'Analyze', feature: 'viewport', prompt: 'Give me a general read of this chart. What do you see?'},
        {label: 'Validate', feature: 'validate', prompt: 'Validate my current trade setup.'},
        {label: 'Challenge', feature: 'challenge', prompt: 'Challenge my current bias. What am I missing?'},
        {label: 'Score', feature: 'chat', prompt: 'Score this asset. How strong is the setup for both long and short?'},
        {label: 'Checklist', feature: 'checklist', prompt: 'Run a pre-trade checklist for this setup.'},
        {label: 'Confluence', feature: 'confluence', prompt: 'Score the indicator confluence for direction bias.'},
        {label: 'Design', feature: 'trade_design', prompt: 'Stress-test my thesis for this asset.'},
        {label: 'Plays', feature: 'chat', prompt: 'How are my active plays looking against the current market? For each play, assess conditions and distance to trigger/invalidation.'},
        {label: 'Patterns', feature: 'patterns', prompt: ''},
    ],
    report: [
        {label: 'Follow Up', feature: 'report_followup', prompt: 'Based on today\'s morning report, what are the key takeaways for my portfolio?'},
        {label: 'Dig Deeper', feature: 'report_followup', prompt: 'Expand on the key themes from the morning report. What should I watch?'},
        {label: 'Play Impact', feature: 'report_followup', prompt: 'How does today\'s report affect my active plays?'},
    ],
    scanner: [
        {label: 'Explain Score', feature: 'chat', prompt: 'Explain the score breakdown for this asset. Why this verdict?'},
        {label: 'Find Setups', feature: 'chat', prompt: 'Which assets have the strongest setups right now?'},
    ],
    dashboard: [
        {label: 'Analyze', feature: 'viewport', prompt: 'Give me a general read of this chart. What do you see?'},
        {label: 'Challenge', feature: 'challenge', prompt: 'Challenge my current bias. What am I missing?'},
    ],
    news: [
        {label: 'Impact Check', feature: 'news_analysis', prompt: 'How does this affect my positions and active plays?'},
        {label: 'Asset Scan', feature: 'news_analysis', prompt: 'Which assets are affected by this news? Pull up their charts.'},
        {label: 'Bull / Bear', feature: 'news_analysis', prompt: 'What\'s the bull and bear case from this headline?'},
        {label: 'Challenge', feature: 'news_analysis', prompt: 'Why might this news not matter? What\'s already priced in?'},
    ],
};

// Guided empty-state actions per page context (mirrors PAGE_ACTIONS with one-liner descriptions)
var GUIDED_ACTIONS = {
    chart: [
        {label: 'Analyze', desc: 'General technical read of the chart', feature: 'viewport', prompt: 'Give me a general read of this chart. What do you see?'},
        {label: 'Validate', desc: 'Assess the trade plan you wrote', feature: 'validate', prompt: 'Validate my current trade setup.'},
        {label: 'Challenge', desc: 'Devil\'s advocate on your bias', feature: 'challenge', prompt: 'Challenge my current bias. What am I missing?'},
        {label: 'Score', desc: 'Rate setup strength long & short', feature: 'chat', prompt: 'Score this asset. How strong is the setup for both long and short?'},
        {label: 'Checklist', desc: 'Pre-trade checklist before entry', feature: 'checklist', prompt: 'Run a pre-trade checklist for this setup.'},
        {label: 'Confluence', desc: 'Score indicator confluence for bias', feature: 'confluence', prompt: 'Score the indicator confluence for direction bias.'},
        {label: 'Design', desc: 'Stress-test your thesis and the levels that matter', feature: 'trade_design', prompt: 'Stress-test my thesis for this asset.'},
        {label: 'Plays', desc: 'Check your active plays vs market', feature: 'chat', prompt: 'How are my active plays looking against the current market? For each play, assess conditions and distance to trigger/invalidation.'},
        {label: 'Patterns', desc: 'Find similar historical patterns', feature: 'patterns', prompt: ''},
    ],
    report: [
        {label: 'Follow Up', desc: 'Key takeaways for your portfolio', feature: 'report_followup', prompt: 'Based on today\'s morning report, what are the key takeaways for my portfolio?'},
        {label: 'Dig Deeper', desc: 'Expand on today\'s key themes', feature: 'report_followup', prompt: 'Expand on the key themes from the morning report. What should I watch?'},
        {label: 'Play Impact', desc: 'How the report affects your plays', feature: 'report_followup', prompt: 'How does today\'s report affect my active plays?'},
    ],
    scanner: [
        {label: 'Explain Score', desc: 'Why this asset scored this way', feature: 'chat', prompt: 'Explain the score breakdown for this asset. Why this verdict?'},
        {label: 'Find Setups', desc: 'Strongest setups right now', feature: 'chat', prompt: 'Which assets have the strongest setups right now?'},
        {label: 'Compare Top 5', desc: 'Compare highest-scoring assets', feature: 'chat', prompt: 'Compare the top 5 highest-scoring assets. What stands out?'},
    ],
    journal: [
        {label: 'Review Week', desc: 'AI summary of last 7 days', feature: 'chat', prompt: 'Review my trading over the last 7 days. What patterns do you see?'},
        {label: 'Pattern Analysis', desc: 'Recurring patterns in your trades', feature: 'chat', prompt: 'Analyze my trade history for recurring patterns — good and bad habits.'},
        {label: 'Biggest Mistakes', desc: 'Most costly errors to fix', feature: 'chat', prompt: 'What are my most costly and recurring trading mistakes?'},
        {label: 'Best Setups', desc: 'Your highest-performing setups', feature: 'chat', prompt: 'Which setup types have been my most profitable? Show win rate and avg R.'},
        {label: 'Win Rate', desc: 'Performance breakdown by category', feature: 'chat', prompt: 'Break down my win rate by asset, direction, timeframe, and setup type.'},
    ],
    plays: [
        {label: 'Check Plays', desc: 'Status of all active plays', feature: 'chat', prompt: 'How are my active plays looking? For each, show condition progress and distance to trigger.'},
        {label: 'Evaluate', desc: 'Re-check conditions vs market', feature: 'chat', prompt: 'Re-evaluate all my active play conditions against the current market.'},
        {label: 'Best Setup', desc: 'Which play is closest to firing?', feature: 'chat', prompt: 'Which of my plays is closest to having all conditions met?'},
        {label: 'Expired Review', desc: 'Review expired/invalidated plays', feature: 'chat', prompt: 'Review my expired and invalidated plays. What can I learn from them?'},
    ],
    positions: [
        {label: 'Risk Check', desc: 'Portfolio risk and exposure', feature: 'chat', prompt: 'Check my current portfolio risk. How exposed am I and where?'},
        {label: 'Exit Plan', desc: 'Review exit strategy for open positions', feature: 'chat', prompt: 'Review the exit strategy for my open positions. Are my TPs and SLs well-placed?'},
        {label: 'Thesis Check', desc: 'Conditions vs your plan', feature: 'chat', prompt: 'For each open position, compare current market conditions with the thesis, levels and invalidation I recorded. Describe what has changed; do not tell me what to do.'},
    ],
    alerts: [
        {label: 'Smart Alert', desc: 'Suggest alerts based on chart levels', feature: 'chat', prompt: 'Suggest smart alerts based on key support/resistance levels and my drawings.'},
        {label: 'Review Active', desc: 'Are your alerts still relevant?', feature: 'chat', prompt: 'Review my active alerts. Are they still relevant or should any be updated?'},
        {label: 'Cleanup', desc: 'Find stale or redundant alerts', feature: 'chat', prompt: 'Find stale, redundant, or already-passed alerts I should clean up.'},
    ],
    stream: [
        {label: 'Summarize', desc: 'What signals are firing right now?', feature: 'chat', prompt: 'Summarize what signals are firing right now across my watchlist.'},
        {label: 'Unusual Activity', desc: 'Anything abnormal in the feed?', feature: 'chat', prompt: 'Is there any unusual activity or abnormal signals in the live stream?'},
    ],
    backtest: [
        {label: 'Interpret', desc: 'Explain backtest results', feature: 'chat', prompt: 'Interpret these backtest results. What do the numbers tell me?'},
        {label: 'Improve', desc: 'Suggestions to improve the strategy', feature: 'chat', prompt: 'How could I improve this backtest strategy? Suggest parameter tweaks or filter changes.'},
    ],
    consensio: [
        {label: 'Trend Check', desc: 'Multi-timeframe MA alignment analysis', feature: 'chat', prompt: 'Analyze the Consensio score for this asset. What does the multi-timeframe MA alignment tell us?'},
        {label: 'Strongest', desc: 'Highest Consensio scores right now', feature: 'chat', prompt: 'Which assets have the highest Consensio scores right now? What stands out?'},
        {label: 'Divergences', desc: 'Where timeframes disagree', feature: 'chat', prompt: 'Find assets where the daily and weekly Consensio signals disagree. Which are most interesting?'},
        {label: 'Flips', desc: 'Recent Consensio changes', feature: 'chat', prompt: 'Which assets had Consensio score changes recently? Any significant trend shifts?'},
    ],
    detail: [
        {label: 'Full Read', desc: 'Complete technical breakdown', feature: 'viewport', prompt: 'Give me a complete technical breakdown — trend, momentum, structure, levels, volume.'},
        {label: 'Scan All', desc: 'Best setups across all indicators', feature: 'chat', prompt: 'Scan all indicators for this asset. Where is the strongest confluence?'},
        {label: 'Compare', desc: 'Compare this asset to the top 5', feature: 'chat', prompt: 'Compare this asset\'s indicator readings to the top 5 strongest assets right now.'},
        {label: 'Weaknesses', desc: 'What indicators are warning?', feature: 'chat', prompt: 'Which indicators are showing warning signs or divergences for this asset?'},
    ],
    dashboard: [
        {label: 'Analyze', desc: 'General technical read of the chart', feature: 'viewport', prompt: 'Give me a general read of this chart. What do you see?'},
        {label: 'Challenge', desc: 'Devil\'s advocate on your bias', feature: 'challenge', prompt: 'Challenge my current bias. What am I missing?'},
    ],
    news: [
        {label: 'Impact Check', desc: 'How this affects your positions', feature: 'news_analysis', prompt: 'How does this affect my positions and active plays?'},
        {label: 'Asset Scan', desc: 'Which assets are impacted', feature: 'news_analysis', prompt: 'Which assets are affected by this news? Pull up their charts.'},
        {label: 'Bull / Bear', desc: 'Both sides of the headline', feature: 'news_analysis', prompt: 'What\'s the bull and bear case from this headline?'},
        {label: 'Challenge', desc: 'Why it might not matter', feature: 'news_analysis', prompt: 'Why might this news not matter? What\'s already priced in?'},
    ],
};

// Cached context bar data
var _aiLastContextSummary = '';
var _aiLastScore = null;
var _aiScoreFetching = false;
var _lastMsgRole = null; // Track role for sender label clustering

/** Update FAB badge — show green dot when asset is loaded */
function _updateFabBadge() {
    var badge = document.getElementById('ai-fab-badge');
    if (!badge) return;
    var hasAsset = !!(typeof klineChartSymbol !== 'undefined' && klineChartSymbol);
    badge.classList.toggle('visible', hasAsset);
}

/** One-time onboarding tooltip above FAB */
function _showOnboardingTooltip() {
    try { if (localStorage.getItem('aiOnboardingSeen')) return; } catch(e) { return; }
    var tip = document.getElementById('ai-onboarding-tooltip');
    if (!tip) return;
    // Show after short delay
    setTimeout(function() {
        tip.classList.add('visible');
        // Dismiss on click
        tip.addEventListener('click', function() {
            tip.classList.remove('visible');
            try { localStorage.setItem('aiOnboardingSeen', '1'); } catch(e) {}
        });
        // Auto-dismiss after 5s
        setTimeout(function() {
            tip.classList.remove('visible');
            try { localStorage.setItem('aiOnboardingSeen', '1'); } catch(e) {}
        }, 5000);
    }, 1500);
}

/** Append sender label if role changed, with optional timestamp */
function _maybeSenderLabel(chatEl, role, timestamp) {
    if (role === _lastMsgRole) return;
    // Add timestamp before the new cluster (if we had a previous cluster)
    if (_lastMsgRole !== null && timestamp) {
        var ts = document.createElement('div');
        ts.className = 'ai-msg-timestamp';
        ts.textContent = typeof timestamp === 'string' ? _formatRelativeTime(timestamp) : _formatRelativeTime(new Date().toISOString());
        chatEl.appendChild(ts);
    }
    _lastMsgRole = role;
    var label = document.createElement('div');
    if (role === 'user') {
        label.className = 'ai-sender-label ai-sender-you';
        label.textContent = 'You';
    } else {
        label.className = 'ai-sender-label ai-sender-ai';
        label.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#f0b429" stroke-width="2.5"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Copilot';
    }
    chatEl.appendChild(label);
}

/** Create typing indicator (three dots) */
function _createTypingIndicator() {
    var div = document.createElement('div');
    div.className = 'ai-panel-msg ai-panel-msg-ai';
    div.innerHTML = '<div class="ai-typing-dots"><span></span><span></span><span></span></div>';
    return div;
}

/** Update context preview chip in input row */
function _updateContextChip() {
    var chip = document.getElementById('ai-context-chip');
    if (!chip) return;
    if (_aiLastContextSummary) {
        var tokMatch = _aiLastContextSummary.match(/Total:\s*(\d+)\s*tok/);
        chip.textContent = 'Context: ~' + (tokMatch ? tokMatch[1] : '?') + ' tok';
        chip.classList.add('visible');
    } else if (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) {
        chip.textContent = 'Context: ~1k tok';
        chip.classList.add('visible');
    } else {
        chip.classList.remove('visible');
    }
}

/** Render guided empty state with action buttons + one-liner descriptions */
function _renderGuidedEmptyState() {
    var chatEl = document.getElementById('ai-panel-chat');
    if (!chatEl) return;
    var ctx = _getCopilotFocus();
    var actions = GUIDED_ACTIONS[ctx] || GUIDED_ACTIONS.dashboard;
    var hasAsset = !!(typeof klineChartSymbol !== 'undefined' && klineChartSymbol);
    var needsAsset = ['viewport', 'validate', 'challenge', 'checklist', 'confluence', 'trade_design', 'patterns'];
    var html = '<div class="ai-panel-empty"><div class="ai-empty-actions">';
    actions.forEach(function(a) {
        var disabled = needsAsset.indexOf(a.feature) !== -1 && !hasAsset;
        html += '<button class="ai-empty-action' + (disabled ? ' disabled' : '') + '" data-feature="' + a.feature + '" data-prompt="' + a.prompt.replace(/"/g, '&quot;') + '"' + (disabled ? ' disabled' : '') + '>';
        html += '<span class="ai-empty-action-label">' + a.label + '</span>';
        html += '<span class="ai-empty-action-desc">' + a.desc + '</span>';
        html += '</button>';
    });
    html += '</div></div>';
    chatEl.innerHTML = html;
    chatEl.querySelectorAll('.ai-empty-action:not(.disabled)').forEach(function(btn) {
        btn.addEventListener('click', function() {
            _aiPanelDoSend(this.getAttribute('data-prompt'), this.getAttribute('data-feature'));
        });
    });
}

/**
 * Master state machine for the AI panel.
 * Three states: 'fab' (closed), 'small' (popup), 'column' (docked).
 */
function setAIPanelState(targetState) {
    if (targetState === _aiPanelState) return;
    var panel = document.getElementById('ai-float');
    if (!panel) return;

    // Lazy init on first open (any non-fab state)
    if (_aiPanelState === 'fab' && targetState !== 'fab') {
        if (!panel._initialized) {
            initAIPanel();
            panel._initialized = true;
            refreshAIPanelBalance();
        }
    }

    var prevState = _aiPanelState;

    // Exit current state
    if (prevState === 'column') {
        _exitColumnState(panel);
    } else if (prevState === 'small') {
        panel.classList.remove('ai-state-small');
    } else if (prevState === 'fab') {
        // nothing to exit
    }

    // Enter target state
    if (targetState === 'column') {
        // Guard: no column on mobile
        if (window.innerWidth <= 768) { targetState = 'small'; }
    }

    if (targetState === 'column') {
        _enterColumnState(panel);
    } else if (targetState === 'small') {
        panel.classList.add('ai-state-small');
        panel.classList.remove('ai-state-column');
        if (panel.parentElement !== document.body) document.body.appendChild(panel);
        // Re-apply font-based size
        _aiApplyFont();
        var fab = document.getElementById('ai-fab');
        if (fab) { fab.style.display = ''; fab.classList.remove('ai-fab-hidden'); }
    } else {
        // fab state — animate out then hide
        if (prevState === 'small') {
            panel.style.animation = 'aiPopupOut 0.15s ease-in forwards';
            setTimeout(function() {
                panel.classList.remove('ai-state-small', 'ai-state-column');
                panel.style.animation = '';
                if (panel.parentElement !== document.body) document.body.appendChild(panel);
            }, 140);
        } else {
            panel.classList.remove('ai-state-small', 'ai-state-column');
            if (panel.parentElement !== document.body) document.body.appendChild(panel);
        }
        var fab = document.getElementById('ai-fab');
        if (fab) { fab.style.display = ''; fab.classList.remove('ai-fab-hidden'); }
    }

    _aiPanelState = targetState;

    // Persist last-open state for / key
    if (targetState !== 'fab') {
        try { localStorage.setItem('aiPanelLastOpen', targetState); } catch(e) {}
        _updateQuickActions();
        _updateContextBar();
        _updateCopilotTitle();
    }

    // Update expand button icon
    _updateExpandBtn();

    // Resize chart if transitioning to/from column
    if ((prevState === 'column' || targetState === 'column') && typeof klineChart !== 'undefined' && klineChart) {
        setTimeout(function() { klineChart.resize(); }, 50);
    }
}

function _enterColumnState(panel) {
    // Save chat scroll
    var chatEl = document.getElementById('ai-panel-chat');
    var scrollPos = chatEl ? chatEl.scrollTop : 0;

    // Insert into split-layout
    if (typeof insertAIColumn === 'function') insertAIColumn(panel);

    panel.classList.remove('ai-state-small');
    panel.classList.add('ai-state-column');

    // Restore saved width
    var savedWidth = null;
    try { savedWidth = localStorage.getItem('aiColumnWidth'); } catch(e) {}
    if (savedWidth) panel.style.width = savedWidth + 'px';

    // Init drag divider
    if (typeof initAIPanelDivider === 'function') initAIPanelDivider();

    // Restore scroll
    if (chatEl) chatEl.scrollTop = scrollPos;

    // Hide FAB
    var fab = document.getElementById('ai-fab');
    if (fab) { fab.classList.add('ai-fab-hidden'); }
}

function _exitColumnState(panel) {
    // Save chat scroll
    var chatEl = document.getElementById('ai-panel-chat');
    var scrollPos = chatEl ? chatEl.scrollTop : 0;

    // Remove from split-layout
    if (typeof removeAIColumn === 'function') removeAIColumn(panel);

    panel.classList.remove('ai-state-column');
    panel.style.width = ''; // Clear inline width

    // Restore scroll
    if (chatEl) chatEl.scrollTop = scrollPos;
}

function _updateExpandBtn() {
    var btn = document.getElementById('ai-win-dock');
    if (!btn) return;
    if (_aiPanelState === 'column') {
        btn.title = 'Undock to popup';
        // Two overlapping diamonds (restore icon)
        btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12"><polygon points="7.5,0.5 11.5,4.5 7.5,8.5 3.5,4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="miter"/><polygon points="4.5,3.5 8.5,7.5 4.5,11.5 0.5,7.5" fill="#161b22" stroke="currentColor" stroke-width="1.4" stroke-linejoin="miter"/></svg>';
    } else {
        btn.title = 'Dock as column';
        btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12"><polygon points="6,1.5 10.5,6 6,10.5 1.5,6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="miter"/></svg>';
    }
}

/**
 * Legacy compatibility wrapper — external code (plays panel, etc.) calls toggleAIPanel().
 */
function toggleAIPanel(forceOpen) {
    if (forceOpen === false) {
        // Close: cascade down one level
        if (_aiPanelState === 'column') setAIPanelState('small');
        else if (_aiPanelState === 'small') setAIPanelState('fab');
    } else if (forceOpen === true || forceOpen === undefined) {
        // Open: go to last-used state or small
        if (_aiPanelState === 'fab') {
            var last = 'small';
            try { last = localStorage.getItem('aiPanelLastOpen') || 'small'; } catch(e) {}
            setAIPanelState(last);
        }
    }
}

/** Track which area the user last interacted with: 'chart', 'drawer', or 'panel'. */
var _copilotLastArea = 'chart';
(function() {
    // Chart area click → focus returns to chart
    document.addEventListener('click', function(e) {
        var chartEl = document.getElementById('kline-chart-container') || document.getElementById('chart-container');
        if (chartEl && chartEl.contains(e.target)) { _copilotLastArea = 'chart'; return; }
        var drawerEl = document.getElementById('chart-drawer');
        if (drawerEl && drawerEl.contains(e.target)) { _copilotLastArea = 'drawer'; return; }
        // News panel — keep news focus while interacting with news cards
        var newsEl = document.getElementById('panel-news-view');
        if (newsEl && newsEl.contains(e.target) && typeof _npActiveArticle !== 'undefined' && _npActiveArticle) { _copilotLastArea = 'news'; return; }
        // Right panel view mode buttons
        if (e.target.closest && e.target.closest('.view-mode-btn, .panel-view-strip, .panel-tabs')) { _copilotLastArea = 'panel'; }
    }, true);
})();

/** Get copilot focus — richer context detection for the AI panel.
 *  Priority: 1) last interaction area, 2) drawer tab, 3) view mode, 4) chart.
 */
function _getCopilotFocus() {
    // 0. News panel — set by Ask AI button on a news article
    if (_copilotLastArea === 'news') {
        return 'news';
    }
    // 1. If user last clicked the chart area, always return chart
    if (_copilotLastArea === 'chart') {
        return 'chart';
    }
    // 2. Bottom drawer tab (if drawer is open and user interacted with it)
    if (_copilotLastArea === 'drawer' && typeof drawerOpen !== 'undefined' && drawerOpen && typeof drawerActiveTab !== 'undefined' && drawerActiveTab) {
        var drawerMap = {position: 'positions', alerts: 'alerts', scanner: 'scanner', stream: 'stream', backtest: 'backtest'};
        if (drawerMap[drawerActiveTab]) return drawerMap[drawerActiveTab];
    }
    // 3. Right panel view mode
    var viewMode = '';
    try { viewMode = localStorage.getItem('panelViewMode') || ''; } catch(e) {}
    if (viewMode === 'news') return 'news';
    if (viewMode === 'journal') return 'journal';
    if (viewMode === 'plays') return 'plays';
    if (viewMode === 'report') return 'report';
    if (viewMode === 'consensio') return 'consensio';
    if (viewMode === 'full') return 'detail';
    // 4. Default
    return 'chart';
}

var _FOCUS_LABELS = {
    chart: 'Chart', journal: 'Journal', plays: 'Plays', report: 'Morning Report',
    positions: 'Positions', alerts: 'Alerts', scanner: 'Scanner',
    stream: 'Radar', backtest: 'Backtest', dashboard: 'Dashboard',
    consensio: 'Consensio', detail: 'Detail View', news: 'News'
};

/** Update the copilot panel title based on current focus */
function _updateCopilotTitle() {
    var el = document.getElementById('ai-panel-title');
    if (!el) return;
    var focus = _getCopilotFocus();
    var asset = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || '';
    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '';
    var assetSuffix = asset ? ' \u00b7 ' + asset + (tf ? ', ' + tf : '') : '';
    if (focus === 'news') {
        var articleTitle = '';
        if (typeof _npActiveArticle !== 'undefined' && _npActiveArticle && _npActiveArticle.title) {
            articleTitle = _npActiveArticle.title;
        }
        el.textContent = 'AI Copilot \u00b7 ' + (articleTitle || 'News');
    } else if (focus === 'chart' || focus === 'consensio' || focus === 'detail') {
        el.textContent = 'AI Copilot' + (asset ? ' \u00b7 ' + asset + (tf ? ', ' + tf : '') : ' \u00b7 ' + (_FOCUS_LABELS[focus] || 'Chart'));
    } else if (focus === 'positions' && asset) {
        el.textContent = 'AI Copilot \u00b7 ' + asset + ' Position';
    } else {
        el.textContent = 'AI Copilot \u00b7 ' + (_FOCUS_LABELS[focus] || 'Chart');
    }
}

function _updateQuickActions() {
    var container = document.getElementById('ai-quick-actions');
    if (!container) return;
    var ctx = _getCopilotFocus();
    var actions = PAGE_ACTIONS[ctx] || PAGE_ACTIONS.dashboard;
    var hasAsset = !!(typeof klineChartSymbol !== 'undefined' && klineChartSymbol);

    container.innerHTML = '';
    actions.forEach(function(a) {
        var btn = document.createElement('button');
        btn.textContent = a.label;
        var needsAsset = ['viewport', 'validate', 'challenge', 'checklist', 'confluence', 'trade_design', 'patterns'].indexOf(a.feature) !== -1;
        if (needsAsset && !hasAsset) {
            btn.disabled = true;
            btn.title = 'Select an asset first';
        } else {
            btn.addEventListener('click', function() {
                container.classList.remove('visible');
                _aiPanelDoSend(a.prompt, a.feature);
            });
        }
        container.appendChild(btn);
    });

    // Mobile long-press preview for quick actions
    if ('ontouchstart' in window || navigator.maxTouchPoints > 0) {
        container.querySelectorAll('button:not(.ai-actions-chevron)').forEach(function(btn) {
            var _lpTimer = null;
            var _lpTip = null;
            btn.addEventListener('touchstart', function(e) {
                var promptText = btn.title || btn.textContent;
                _lpTimer = setTimeout(function() {
                    e.preventDefault();
                    _lpTip = document.createElement('div');
                    _lpTip.className = 'ai-longpress-tooltip';
                    _lpTip.textContent = promptText;
                    var rect = btn.getBoundingClientRect();
                    _lpTip.style.left = rect.left + 'px';
                    _lpTip.style.top = (rect.top - 40) + 'px';
                    document.body.appendChild(_lpTip);
                }, 500);
            }, { passive: false });
            btn.addEventListener('touchend', function() {
                clearTimeout(_lpTimer);
                if (_lpTip) { _lpTip.remove(); _lpTip = null; }
            });
            btn.addEventListener('touchmove', function() {
                clearTimeout(_lpTimer);
                if (_lpTip) { _lpTip.remove(); _lpTip = null; }
            });
        });
    }
}

function _updateContextBar() {
    var bar = document.getElementById('ai-context-bar');
    var pill = document.getElementById('ai-context-pill');

    // Update score pill in header (compact)
    if (pill) {
        if (_aiLastScore && typeof klineChartSymbol !== 'undefined' && klineChartSymbol) {
            var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';
            pill.textContent = klineChartSymbol + ' ' + tf + ' \u00b7 ' + _aiLastScore.total + '/12 ' + _aiLastScore.verdict;
            pill.classList.add('visible');
            pill.classList.remove('bullish', 'bearish', 'neutral');
            if (_aiLastScore.total >= 8) pill.classList.add('bullish');
            else if (_aiLastScore.total <= 4) pill.classList.add('bearish');
            else pill.classList.add('neutral');
        } else if (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) {
            pill.textContent = klineChartSymbol;
            pill.classList.add('visible');
            pill.classList.remove('bullish', 'bearish', 'neutral');
        } else {
            pill.classList.remove('visible');
        }
    }

    // Update full context bar (hidden by default, shown when expanded)
    if (bar) {
        var parts = [];
        if (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) {
            var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';
            parts.push(klineChartSymbol + ' ' + tf);
        }
        if (_aiLastScore) parts.push(_aiLastScore.total + '/12 ' + _aiLastScore.verdict);
        if (_aiLastContextSummary) {
            var tokMatch = _aiLastContextSummary.match(/Total:\s*(\d+)\s*tok/);
            if (tokMatch) parts.push(tokMatch[1] + ' tok');
        }
        // Only set text if expanded — otherwise keep it collapsed
        if (bar.classList.contains('expanded')) {
            bar.textContent = _aiLastContextSummary || (parts.length ? parts.join(' \u00b7 ') : 'No asset selected');
        }
    }

    // Fetch quick score if asset available and no cached score
    if (typeof klineChartSymbol !== 'undefined' && klineChartSymbol && !_aiLastScore && !_aiScoreFetching) {
        _aiScoreFetching = true;
        fetch('/api/ai/quick-score?asset=' + klineChartSymbol, { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) {
                _aiScoreFetching = false;
                if (data && data.total !== undefined) {
                    _aiLastScore = data;
                    _updateContextBar();
                }
            }).catch(function() { _aiScoreFetching = false; });
    }
}

function cycleViewMode(direction) {
    if (typeof setViewMode !== 'function') return;

    // If panel is collapsed, expand to first/last mode
    if (typeof _panelCollapsed !== 'undefined' && _panelCollapsed) {
        toggleRightPanel();
        var target = direction > 0 ? VIEW_MODE_ORDER[0] : VIEW_MODE_ORDER[VIEW_MODE_ORDER.length - 1];
        setViewMode(target);
        return;
    }

    var current = localStorage.getItem('panelViewMode') || 'consensio';
    var idx = VIEW_MODE_ORDER.indexOf(current);
    if (idx === -1) idx = 1;
    var next = idx + direction;

    // Past the end/beginning → collapse
    if (next >= VIEW_MODE_ORDER.length || next < 0) {
        if (typeof toggleRightPanel === 'function') toggleRightPanel();
        return;
    }

    setViewMode(VIEW_MODE_ORDER[next]);
}

/**
 * Initialize AI panel event listeners (chat + profile only).
 * Called once when the floating AI panel is first opened.
 */
function initAIPanel() {
    if (_aiPanelInitDone) return;
    _aiPanelInitDone = true;

    var aiFloat = document.getElementById('ai-float');
    if (!aiFloat) return;

    // Close button — cascade: column��small, small→fab
    // (Old close button removed — window controls bound in DOMContentLoaded)

    // Context bar expand/collapse (from pill click or bar click)
    var ctxBar = document.getElementById('ai-context-bar');
    var ctxPill = document.getElementById('ai-context-pill');
    function _toggleContextBar() {
        if (!ctxBar) return;
        if (ctxBar.classList.contains('expanded')) {
            ctxBar.classList.remove('expanded');
        } else if (_aiLastContextSummary || _aiLastScore) {
            ctxBar.textContent = _aiLastContextSummary || ('Score: ' + (_aiLastScore ? _aiLastScore.total + '/12 ' + _aiLastScore.verdict : 'N/A'));
            ctxBar.classList.add('expanded');
        }
    }
    if (ctxBar) ctxBar.addEventListener('click', _toggleContextBar);
    if (ctxPill) ctxPill.addEventListener('click', _toggleContextBar);

    // Track focus + asset + timeframe changes to keep the open panel in sync
    var _lastFocus = _getCopilotFocus();
    var _lastAsset = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || '';
    var _lastTf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '';
    setInterval(function() {
        if (document.hidden) return;
        if (_aiPanelState === 'fab') return;
        var f = _getCopilotFocus();
        var a = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || '';
        var t = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '';
        var focusChanged = f !== _lastFocus;
        var symbolChanged = a !== _lastAsset;
        var tfChanged = t !== _lastTf;
        if (focusChanged || symbolChanged || tfChanged) {
            _lastFocus = f;
            _lastAsset = a;
            _lastTf = t;
            // Asset changed → invalidate cached score + context summary so the
            // pill doesn't show stale data alongside the new symbol. The next
            // _updateContextBar() call will refetch the quick-score automatically.
            if (symbolChanged) {
                _aiLastScore = null;
                _aiScoreFetching = false;
                _aiLastContextSummary = '';
                _updateFabBadge();
            }
            _updateCopilotTitle();
            _updateContextBar();
            // Quick actions only depend on focus, not asset/tf — skip if only the asset changed
            if (focusChanged) _updateQuickActions();
            // Re-render empty state if no conversation loaded
            if (!_aiConversationId) {
                var chatEl = document.getElementById('ai-panel-chat');
                if (chatEl && chatEl.querySelector('.ai-panel-empty')) {
                    _renderGuidedEmptyState();
                }
            }
        }
    }, 500);

    // (Quick actions now in dropdown, toggled via Actions button in toolbar)

    // "New Chat" button (in toolbar)
    var newChatBtn = document.getElementById('ai-new-chat');
    if (newChatBtn) newChatBtn.addEventListener('click', function() {
        _aiNewChat();
        _toggleHistoryPanel(false);
    });

    // Font size applied via inline onclick on buttons + DOMContentLoaded
    _aiApplyFont();

    // Chat: Send button
    var sendBtn = document.getElementById('ai-panel-send');
    if (sendBtn) {
        sendBtn.addEventListener('click', function() { aiPanelSend(); });
    }

    // Chat: Textarea
    var textarea = document.getElementById('ai-panel-textarea');
    if (textarea) {
        textarea.addEventListener('keydown', function(e) {
            e.stopPropagation();
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                if (!aiPanelStreaming) aiPanelSend();
            }
            if (e.key === 'Escape') {
                e.stopImmediatePropagation();
                textarea.blur();
                if (_aiPanelState === 'column') setAIPanelState('small');
                else setAIPanelState('fab');
            }
        });
        textarea.addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 80) + 'px';
        });
    }

    // Trading Profile — now links to /settings#trading-profile (handled by <a> tag)

    // Context Composer
    if (typeof initContextComposer === 'function') initContextComposer();

    // Context preview chip — click to show/hide popover
    var ctxChip = document.getElementById('ai-context-chip');
    if (ctxChip) {
        ctxChip.addEventListener('click', function(e) {
            e.stopPropagation();
            var existing = ctxChip.parentElement.querySelector('.ai-context-popover');
            if (existing) { existing.remove(); return; }
            if (!_aiLastContextSummary) return;
            var pop = document.createElement('div');
            pop.className = 'ai-context-popover';
            pop.textContent = _aiLastContextSummary;
            ctxChip.style.position = 'relative';
            ctxChip.appendChild(pop);
            // Close on outside click
            setTimeout(function() {
                document.addEventListener('click', function _closePop() {
                    pop.remove();
                    document.removeEventListener('click', _closePop);
                }, { once: true });
            }, 0);
        });
    }
    _updateContextChip();

    // Tab-cycle through quick actions (roving tabindex)
    var aiFloatEl = document.getElementById('ai-float');
    if (aiFloatEl) {
        aiFloatEl.addEventListener('keydown', function(e) {
            if (e.key !== 'Tab' && e.key !== 'Enter') return;
            var ta = document.getElementById('ai-panel-textarea');
            if (ta && ta.value.trim() !== '') return; // only when textarea empty
            var actions = Array.from(document.querySelectorAll('#ai-quick-actions button:not(.ai-actions-chevron):not([disabled])'));
            if (!actions.length) return;
            var focused = document.activeElement;
            var idx = actions.indexOf(focused);
            if (e.key === 'Tab') {
                e.preventDefault();
                var next = e.shiftKey ? (idx <= 0 ? actions.length - 1 : idx - 1) : (idx >= actions.length - 1 ? 0 : idx + 1);
                actions[next].focus();
            } else if (e.key === 'Enter' && idx >= 0) {
                e.preventDefault();
                focused.click();
            }
        });
    }

    // Restore last conversation on refresh (or show guided prompts)
    if (_aiConversationId) {
        _aiLoadConversation(_aiConversationId);
    } else {
        _renderGuidedEmptyState();
    }
}

function _aiSetConversationId(id) {
    _aiConversationId = id;
    try { if (id) localStorage.setItem('aiConversationId', id); else localStorage.removeItem('aiConversationId'); } catch(e) {}
}

function _aiNewChat() {
    _aiSetConversationId(null);
    _lastMsgRole = null;
    _renderGuidedEmptyState();
    _updateCopilotTitle();
    _updateQuickActions();
}

var TRADFI_ASSETS = ['SPX','QQQ','GOLD','SILVER','OIL','NATGAS','COPPER','DXY','MAGS','XLE','CORN','WHEAT','SOYBEANS'];

function _formatRelativeTime(dateStr) {
    if (!dateStr) return '';
    if (dateStr === 'now') return 'just now';
    var d = new Date(dateStr.replace(' ', 'T') + (dateStr.includes('Z') ? '' : 'Z'));
    var diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 172800) return 'Yesterday';
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function _toggleHistoryPanel(show) {
    var histPanel = document.getElementById('ai-history-panel');
    var chatEl = document.getElementById('ai-panel-chat');
    var inputEl = document.getElementById('ai-panel-input-wrapper');
    var actionsEl = document.getElementById('ai-quick-actions');
    var ctxBar = document.getElementById('ai-context-bar');
    var composerEl = document.getElementById('composer-panel');
    if (!histPanel) return;
    if (show) {
        histPanel.style.display = 'flex';
        if (chatEl) chatEl.style.display = 'none';
        if (inputEl) inputEl.style.display = 'none';
        if (actionsEl) actionsEl.style.display = 'none';
        if (ctxBar) ctxBar.style.display = 'none';
        if (composerEl) composerEl.style.display = 'none';
        _aiRefreshHistoryPanel();
    } else {
        histPanel.style.display = 'none';
        if (chatEl) chatEl.style.display = '';
        if (inputEl) inputEl.style.display = '';
        if (actionsEl) actionsEl.style.display = '';
        if (ctxBar) ctxBar.style.display = '';
    }
}

function _aiRefreshHistoryPanel() {
    var list = document.getElementById('ai-history-list');
    if (!list) return;
    list.innerHTML = '<div style="padding:12px;color:#484f58;font-size:0.6rem">Loading...</div>';
    fetch('/api/ai/conversations', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data || !data.conversations) return;
            list.innerHTML = '';
            data.conversations.forEach(function(c) {
                var item = document.createElement('div');
                item.className = 'ai-history-item' + (c.id === _aiConversationId ? ' active' : '');
                // Meta row: asset badge + feature + time
                var meta = document.createElement('div');
                meta.className = 'ai-history-meta';
                if (c.primary_asset) {
                    var badge = document.createElement('span');
                    badge.className = 'ai-history-asset' + (TRADFI_ASSETS.indexOf(c.primary_asset) !== -1 ? ' tradfi' : '');
                    badge.textContent = c.primary_asset + (c.timeframe ? ', ' + c.timeframe : '');
                    meta.appendChild(badge);
                }
                var time = document.createElement('span');
                time.className = 'ai-history-time';
                time.textContent = _formatRelativeTime(c.updated_at);
                meta.appendChild(time);
                item.appendChild(meta);
                // Title row
                var title = document.createElement('div');
                title.className = 'ai-history-text';
                title.textContent = c.title || 'Chat ' + c.id;
                item.appendChild(title);
                // Click to load
                item.addEventListener('click', function() {
                    _aiLoadConversation(c.id);
                    _toggleHistoryPanel(false);
                });
                list.appendChild(item);
            });
            if (data.conversations.length === 0) {
                list.innerHTML = '<div style="padding:16px;color:#484f58;font-size:0.6rem;text-align:center">No conversations yet</div>';
            }
        }).catch(function() {
            list.innerHTML = '<div style="padding:12px;color:#f85149;font-size:0.6rem">Failed to load</div>';
        });
}

function _aiLoadConversation(convId) {
    var chatEl = document.getElementById('ai-panel-chat');
    if (!chatEl) return;
    chatEl.innerHTML = '<div class="ai-panel-empty">Loading...</div>';

    fetch('/api/ai/conversations/' + convId + '/messages', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data || !data.messages) return;
            chatEl.innerHTML = '';
            _aiSetConversationId(convId);
            _lastMsgRole = null;
            data.messages.forEach(function(msg) {
                _maybeSenderLabel(chatEl, msg.role, msg.created_at);
                var div = document.createElement('div');
                if (msg.role === 'user') {
                    div.className = 'ai-panel-msg ai-panel-msg-user';
                    div.textContent = msg.content;
                } else {
                    div.className = 'ai-panel-msg ai-panel-msg-ai';
                    var content = document.createElement('div');
                    content.className = 'ai-msg-content';
                    content.innerHTML = _aiRenderMarkdown(msg.content);
                    div.appendChild(content);
                }
                chatEl.appendChild(div);
            });
            chatEl.scrollTop = chatEl.scrollHeight;
        }).catch(function() {
            chatEl.innerHTML = '<div class="ai-panel-empty">Failed to load chat</div>';
        });
}

/**
 * Initialize Journal panel event listeners.
 * Called once when Journal view mode is first activated.
 */
function initJournalPanel() {
    _journalLoaded = true;

    var logBtn = document.getElementById('journal-log-trade');
    if (logBtn) logBtn.addEventListener('click', _journalShowLogForm);

    var importBtn = document.getElementById('journal-import-drawings');
    if (importBtn) importBtn.addEventListener('click', _journalImportDrawings);

    // Onboarding CTAs (zero-trades state). The log form renders inside the
    // Positions tab, so switch there first or the form opens invisibly.
    var obLogBtn = document.getElementById('jp-onboarding-log');
    if (obLogBtn) obLogBtn.addEventListener('click', function() {
        _switchJournalTab('positions');
        _journalShowLogForm();
    });
    var obImportBtn = document.getElementById('jp-onboarding-import');
    if (obImportBtn) obImportBtn.addEventListener('click', _journalImportDrawings);

    var reviewBtn = document.getElementById('journal-weekly-review');
    if (reviewBtn) reviewBtn.addEventListener('click', _journalWeeklyReview);

    // Analytics toggle
    var analyticsBtn = document.getElementById('journal-toggle-analytics');
    if (analyticsBtn) analyticsBtn.addEventListener('click', _toggleAnalytics);
    var analyticsDays = document.getElementById('journal-analytics-days');
    if (analyticsDays) analyticsDays.addEventListener('change', function() {
        _loadAnalytics(parseInt(this.value));
    });

    // Kelly inputs
    var kellyAccount = document.getElementById('kelly-account-size');
    var kellyFraction = document.getElementById('kelly-fraction');
    var kellyEntry = document.getElementById('kelly-entry');
    var kellyStop = document.getElementById('kelly-stop');
    if (kellyAccount) kellyAccount.addEventListener('input', function() {
        localStorage.setItem('carrac_account_size', this.value);
        _updateKellyResult();
    });
    if (kellyFraction) kellyFraction.addEventListener('change', function() { _updateKellyResult(); });
    if (kellyEntry) kellyEntry.addEventListener('input', function() { _updateKellyTrade(); });
    if (kellyStop) kellyStop.addEventListener('input', function() { _updateKellyTrade(); });
    var savedAccount = localStorage.getItem('carrac_account_size');
    if (savedAccount && kellyAccount) kellyAccount.value = savedAccount;

    // Tab switching
    _initJournalTabs();

    // Dashboard data (sparkline, KPIs, calendar)
    _loadDashboardData();

    // Poll for symbol changes while journal panel is visible
    // Use visibility-aware polling to avoid wasted checks
    if (window._journalPollTimer) clearInterval(window._journalPollTimer);
    window._journalPollTimer = setInterval(function() {
        var journalView = document.getElementById('panel-journal-view');
        if (!journalView || journalView.style.display === 'none' || document.hidden) return;
        _refreshJournalAssetTrades();
    }, 2000);
}

// ============================================================
// Journal Tabs
// ============================================================

function _initJournalTabs() {
    var bar = document.getElementById('jp-tab-bar');
    if (!bar) return;
    var tabs = bar.querySelectorAll('.jp-tab');
    var savedTab = localStorage.getItem('jp_active_tab') || 'positions';
    // Restore saved tab
    _switchJournalTab(savedTab);
    tabs.forEach(function(tab) {
        tab.addEventListener('click', function() {
            _switchJournalTab(this.getAttribute('data-jp-tab'));
        });
    });
}

function _switchJournalTab(tabName) {
    var bar = document.getElementById('jp-tab-bar');
    if (!bar) return;
    var tabs = bar.querySelectorAll('.jp-tab');
    tabs.forEach(function(t) {
        t.classList.toggle('active', t.getAttribute('data-jp-tab') === tabName);
    });
    ['positions', 'history', 'tools'].forEach(function(name) {
        var panel = document.getElementById('jp-tab-' + name);
        if (panel) {
            panel.style.display = name === tabName ? '' : 'none';
            if (name === tabName) panel.classList.add('active');
            else panel.classList.remove('active');
        }
    });
    localStorage.setItem('jp_active_tab', tabName);
    // Reset scroll
    var scroll = document.querySelector('.panel-journal-view .jp-scroll');
    if (scroll) scroll.scrollTop = 0;
}

// ============================================================
// Dashboard: Sparkline + KPIs + Mini Calendar + Live P&L
// ============================================================

var _dashboardAnalytics = null;
var _dashboardCalData = {};

function _loadDashboardData() {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;
    var headers = {'X-CSRFToken': csrf};

    // Equity curve
    fetch('/api/journal/equity-curve?days=90', {headers: headers})
    .then(function(r) { return r.json(); })
    .then(function(data) {
        _renderSparkline(data.curve || []);
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/journal/equity-curve failed — rendered as empty', String((e && e.message) || e)); });

    // Analytics for KPIs
    fetch('/api/ai/journal/analytics?days=30', {headers: headers})
    .then(function(r) { return r.json(); })
    .then(function(data) {
        _dashboardAnalytics = data;
        _renderKpiRow(data);
        _renderLivePnl();
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/ai/journal/analytics failed — rendered as empty', String((e && e.message) || e)); });

    // Calendar (current + previous month for 28-day lookback)
    var now = new Date();
    var curYear = now.getFullYear(), curMonth = now.getMonth() + 1;
    var prevMonth = curMonth === 1 ? 12 : curMonth - 1;
    var prevYear = curMonth === 1 ? curYear - 1 : curYear;

    var calPromises = [
        fetch('/api/journal/calendar?year=' + curYear + '&month=' + curMonth, {headers: headers}).then(function(r) { return r.json(); }),
        fetch('/api/journal/calendar?year=' + prevYear + '&month=' + prevMonth, {headers: headers}).then(function(r) { return r.json(); })
    ];
    Promise.all(calPromises).then(function(results) {
        _dashboardCalData = {};
        results.forEach(function(res) {
            var days = res.days || {};
            Object.keys(days).forEach(function(k) { _dashboardCalData[k] = days[k]; });
        });
        _renderMiniCalendar();
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('journal', 'dashboard calendar failed — mini calendar stays empty', String((e && e.message) || e)); });
}

// ── Sparkline (Canvas 2D) ──

function _renderSparkline(curve) {
    var canvas = document.getElementById('jp-sparkline');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    var dpr = window.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    var cssW = rect.width || 140, cssH = 36;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);

    if (curve.length < 2) {
        ctx.font = '9px JetBrains Mono, monospace';
        ctx.fillStyle = '#30363d';
        ctx.textAlign = 'center';
        ctx.fillText('No trades', cssW / 2, cssH / 2 + 3);
        return;
    }

    var rValues = curve.map(function(p) { return p.cumulative_r; });
    var minR = Math.min(0, Math.min.apply(null, rValues));
    var maxR = Math.max(0, Math.max.apply(null, rValues));
    var range = maxR - minR || 1;
    var pad = {t: 3, r: 4, b: 3, l: 2};
    var cW = cssW - pad.l - pad.r, cH = cssH - pad.t - pad.b;

    function yPos(val) { return pad.t + cH - ((val - minR) / range) * cH; }
    function xPos(i) { return pad.l + (i / (curve.length - 1)) * cW; }

    var lastR = rValues[rValues.length - 1];
    var lineColor = lastR >= 0 ? '#3fb950' : '#ef5350';
    var fillColor = lastR >= 0 ? 'rgba(63,185,80,0.12)' : 'rgba(239,83,80,0.12)';

    // Zero line
    var zeroY = yPos(0);
    ctx.strokeStyle = '#21262d';
    ctx.lineWidth = 0.5;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(pad.l, zeroY);
    ctx.lineTo(pad.l + cW, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Area fill
    ctx.beginPath();
    ctx.moveTo(xPos(0), zeroY);
    for (var i = 0; i < rValues.length; i++) ctx.lineTo(xPos(i), yPos(rValues[i]));
    ctx.lineTo(xPos(rValues.length - 1), zeroY);
    ctx.closePath();
    ctx.fillStyle = fillColor;
    ctx.fill();

    // Drawdown area
    var peakValues = [], runPeak = 0;
    for (var i = 0; i < rValues.length; i++) {
        runPeak = Math.max(runPeak, rValues[i]);
        peakValues.push(runPeak);
    }
    ctx.beginPath();
    ctx.moveTo(xPos(0), yPos(peakValues[0]));
    for (var i = 0; i < rValues.length; i++) ctx.lineTo(xPos(i), yPos(peakValues[i]));
    for (var i = rValues.length - 1; i >= 0; i--) ctx.lineTo(xPos(i), yPos(rValues[i]));
    ctx.closePath();
    ctx.fillStyle = 'rgba(239,83,80,0.10)';
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(xPos(0), yPos(rValues[0]));
    for (var i = 1; i < rValues.length; i++) ctx.lineTo(xPos(i), yPos(rValues[i]));
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // End value
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.fillStyle = lineColor;
    ctx.textAlign = 'right';
    ctx.fillText((lastR >= 0 ? '+' : '') + lastR.toFixed(1) + 'R', cssW - pad.r, pad.t + 9);
}

// ── KPI chips ──

function _renderKpiRow(analytics) {
    var el = document.getElementById('jp-kpi-row');
    if (!el) return;
    if (!analytics || !analytics.trades) {
        el.innerHTML = '';
        return;
    }
    var wr = analytics.win_rate || 0;
    var exp = analytics.expectancy;
    var pf = analytics.profit_factor;
    var totalR = 0;
    if (analytics.avg_rr && analytics.trades) totalR = analytics.avg_rr * analytics.trades;

    function chip(label, value, color) {
        return '<div class="jp-kpi-chip"><span class="jp-kpi-chip-label">' + label +
               '</span><span class="jp-kpi-chip-value" style="color:' + color + '">' + value + '</span></div>';
    }

    var wrColor = wr >= 50 ? '#26a69a' : '#ef5350';
    var expColor = exp != null && exp >= 0 ? '#26a69a' : '#ef5350';
    var pfStr = pf != null ? pf.toFixed(1) : '--';
    var expStr = exp != null ? (exp >= 0 ? '+' : '') + exp.toFixed(2) + 'R' : '--';
    var netStr = (totalR >= 0 ? '+' : '') + totalR.toFixed(1) + 'R';
    var netColor = totalR >= 0 ? '#26a69a' : '#ef5350';

    var chips = chip('WR', wr.toFixed(0) + '%', wrColor) +
                chip('Exp', expStr, expColor) +
                chip('PF', pfStr, '#d1d4dc') +
                chip('Net', netStr, netColor);

    // Streak — current_streak is {type: 'win'|'loss', count: N}
    var streak = analytics.current_streak;
    if (streak && streak.count) {
        var streakColor = streak.type === 'win' ? '#26a69a' : '#ef5350';
        var streakLabel = streak.type === 'win' ? 'W' : 'L';
        chips += chip('Streak', streak.count + streakLabel, streakColor);
    }

    // Max drawdown (in R)
    if (analytics.max_drawdown != null) {
        chips += chip('MaxDD', analytics.max_drawdown.toFixed(1) + 'R', '#ef5350');
    }

    el.innerHTML = chips;
}

// ── Mini Calendar Heatmap ──

function _renderMiniCalendar() {
    var el = document.getElementById('jp-mini-cal');
    if (!el) return;

    var now = new Date();
    var today = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');

    // Build 28-day array ending today
    var cells = [];
    for (var i = 27; i >= 0; i--) {
        var d = new Date(now);
        d.setDate(d.getDate() - i);
        var dateStr = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
        cells.push(dateStr);
    }

    // Find the max absolute R for intensity scaling
    var maxAbsR = 0;
    cells.forEach(function(dateStr) {
        var day = _dashboardCalData[dateStr];
        if (day && day.net_r != null) maxAbsR = Math.max(maxAbsR, Math.abs(day.net_r));
    });
    if (maxAbsR === 0) maxAbsR = 1;

    // Day-of-week headers
    var dayNames = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
    var html = '';
    dayNames.forEach(function(d) {
        html += '<div class="jp-mini-cal-hdr">' + d + '</div>';
    });

    // Pad leading cells to align first day to correct weekday
    var firstDate = new Date(cells[0]);
    var dow = firstDate.getDay(); // 0=Sun, 1=Mon...
    var mondayOffset = dow === 0 ? 6 : dow - 1; // Convert to Mon=0
    for (var i = 0; i < mondayOffset; i++) {
        html += '<div class="jp-cal-cell cal-future"></div>';
    }

    cells.forEach(function(dateStr) {
        var day = _dashboardCalData[dateStr];
        var cls = 'jp-cal-cell';
        var title = '';
        var isFuture = dateStr > today;

        if (isFuture) {
            cls += ' cal-future';
        } else if (day && day.net_r != null && day.net_r !== 0) {
            var absR = Math.abs(day.net_r);
            var intensity = absR / maxAbsR;
            var level = intensity > 0.66 ? 3 : intensity > 0.33 ? 2 : 1;
            cls += day.net_r > 0 ? ' cal-pos-' + level : ' cal-neg-' + level;
            title = dateStr.substring(5) + ': ' + (day.net_r > 0 ? '+' : '') + day.net_r.toFixed(1) + 'R (' + day.trades + ' trades)';
        }
        if (dateStr === today) cls += ' cal-today';

        html += '<div class="' + cls + '"' + (title ? ' title="' + title + '"' : '') + '></div>';
    });

    el.innerHTML = html;
}

// ── Live P&L Summary ──

var _journalOpenCache = [];

var _journalStatsCache = null;

function _renderLivePnl() {
    var el = document.getElementById('jp-live-pnl');
    if (!el) return;

    var analytics = _dashboardAnalytics;
    var open = _journalOpenCache || [];
    var stats = _journalStatsCache;
    var parts = [];

    // Stats summary (30d, W/L, WR%, total R)
    if (stats && stats.trades) {
        var wr = stats.win_rate || 0;
        var wrColor = wr >= 50 ? '#26a69a' : '#ef5350';
        var totalR = (stats.avg_rr && stats.trades) ? stats.avg_rr * stats.trades : 0;
        var totalRColor = totalR >= 0 ? '#26a69a' : '#ef5350';
        var totalRStr = (totalR >= 0 ? '+' : '') + totalR.toFixed(1) + 'R';
        parts.push('<span class="jp-live-chip" style="color:#30363d;">30d</span>');
        parts.push('<span class="jp-live-chip"><span class="jp-live-chip-val">' + stats.wins + 'W/' + stats.losses + 'L</span></span>');
        parts.push('<span class="jp-live-chip"><span class="jp-live-chip-val" style="color:' + wrColor + '">' + wr + '%</span></span>');
        parts.push('<span class="jp-live-chip"><span class="jp-live-chip-val" style="color:' + totalRColor + '">' + totalRStr + '</span></span>');
    }

    // Today's R from calendar data
    var now = new Date();
    var todayStr = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
    var todayData = _dashboardCalData[todayStr];
    if (todayData && todayData.net_r != null && todayData.net_r !== 0) {
        var rColor = todayData.net_r >= 0 ? '#26a69a' : '#ef5350';
        parts.push('<span class="jp-live-chip">Today <span class="jp-live-chip-val" style="color:' + rColor + '">' +
            (todayData.net_r >= 0 ? '+' : '') + todayData.net_r.toFixed(1) + 'R</span></span>');
    }

    // Open position count
    if (open.length > 0) {
        parts.push('<span class="jp-live-chip">Open <span class="jp-live-chip-val">' + open.length + '</span></span>');
    }

    // Current streak
    if (analytics && analytics.current_streak && analytics.current_streak.count > 0) {
        var cs = analytics.current_streak;
        var badgeCls = cs.type === 'win' ? 'jp-streak-win' : 'jp-streak-loss';
        var badgeText = cs.count + (cs.type === 'win' ? 'W' : 'L');
        parts.push('<span class="jp-streak-badge ' + badgeCls + '">' + badgeText + '</span>');
    }

    el.innerHTML = parts.join('');
}

// ============================================================
// Chat Tab (unchanged logic from before)
// ============================================================

function aiPanelSend() {
    var textarea = document.getElementById('ai-panel-textarea');
    if (!textarea) return;
    var text = textarea.value.trim();
    if (!text || aiPanelStreaming) return;
    textarea.value = '';
    textarea.style.height = 'auto';
    _aiPanelDoSend(text, 'chat');
}

function _aiPanelDoSend(message, feature) {
    // Patterns: local computation, not LLM — intercept before chat flow
    if (feature === 'patterns') {
        _patternsInChat();
        return;
    }

    var asset = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || '';
    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';
    var chatEl = document.getElementById('ai-panel-chat');
    if (!chatEl) return;

    var empty = chatEl.querySelector('.ai-panel-empty');
    if (empty) empty.remove();

    _maybeSenderLabel(chatEl, 'user', 'now');
    var userDiv = document.createElement('div');
    userDiv.className = 'ai-panel-msg ai-panel-msg-user';
    userDiv.textContent = message;
    chatEl.appendChild(userDiv);

    _maybeSenderLabel(chatEl, 'assistant', 'now');
    // Typing indicator (replaced when first text chunk arrives)
    var typingDiv = _createTypingIndicator();
    typingDiv.id = '_ai-typing';
    chatEl.appendChild(typingDiv);

    var aiDiv = document.createElement('div');
    aiDiv.className = 'ai-panel-msg ai-panel-msg-ai';
    aiDiv.style.display = 'none';
    var contentEl = document.createElement('div');
    contentEl.className = 'ai-msg-content';
    aiDiv.appendChild(contentEl);
    chatEl.appendChild(aiDiv);
    chatEl.scrollTop = chatEl.scrollHeight;

    var _aiLoadingCleared = false;
    aiPanelStreaming = true;
    _setAIPanelSendState(false);

    // Source-level guard for the recurring stuck "thinking…" indicator. If the SSE
    // stream zombies (half-open TCP after a tab/device sleep), reader.read() never
    // resolves → finish() never runs → aiPanelStreaming stays true forever and the
    // typing indicator strands (the resume sweep even deliberately spares a "live"
    // stream). Abort after a window of total silence so the read promise rejects →
    // .catch → finish() cleans up. Every chunk bumps the deadline AND a shared
    // timestamp the universal reaper reads, so a long legit answer streams freely.
    var _aiAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var _aiIdleTimer = null;
    var AI_STREAM_IDLE_MS = 75000;
    function _bumpAiActivity() {
        window.__aiLastActivityAt = Date.now();
        if (_aiIdleTimer) clearTimeout(_aiIdleTimer);
        _aiIdleTimer = setTimeout(function () {
            try { if (window.carracLog) window.carracLog('ai', 'stream idle-abort: no data for ' + (AI_STREAM_IDLE_MS / 1000) + 's'); } catch (e) {}
            try { if (_aiAbort) _aiAbort.abort(); } catch (e) {}
        }, AI_STREAM_IDLE_MS);
    }
    _bumpAiActivity(); // arm immediately so a hang before the first chunk is caught too

    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    var fullText = '';
    var contextSummary = '';

    // Extract position drawings from localStorage for AI context
    var clientDrawings = [];
    try {
        var allDraw = JSON.parse(localStorage.getItem('klineDrawings') || '{}');
        var assetDraw = allDraw[asset] || [];
        assetDraw.forEach(function(d) {
            if (d.name === 'position' || d.name === 'longPosition' || d.name === 'shortPosition') {
                var pts = (d.points || []).map(function(p) { return { value: p.value, timestamp: p.timestamp }; });
                if (pts.length >= 2) {
                    var ext = d.extendData || {};
                    clientDrawings.push({
                        drawing_type: d.name,
                        points: pts,
                        label: ext.label || '',
                        tps: ext.tps || []
                    });
                }
            }
        });
    } catch(e) {}

    // Detect copilot focus for context assembly + tool scoping
    var pageCtx = _getCopilotFocus();

    // Create conversation if needed, then send
    var sendWithConv = function(convId) {
        var payload = {
            message: message,
            feature: feature,
            asset: asset,
            timeframe: tf,
            conversation_id: convId,
        };
        if (clientDrawings.length > 0) payload.client_drawings = clientDrawings;
        payload.page_context = pageCtx;
        // Context Composer: include recipe_id and thesis_text if available
        if (typeof getComposerRecipeId === 'function') {
            var rid = getComposerRecipeId();
            if (rid) payload.recipe_id = rid;
        }
        if (typeof getComposerThesis === 'function') {
            var thesis = getComposerThesis();
            if (thesis) payload.thesis_text = thesis;
        }
        // News article context: include when copilot focus is news
        if (pageCtx === 'news' && typeof _npGetActiveArticleContext === 'function') {
            var newsCtx = _npGetActiveArticleContext();
            if (newsCtx) payload.news_context = JSON.stringify(newsCtx);
        }
        fetch('/api/ai/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
            body: JSON.stringify(payload),
            signal: _aiAbort ? _aiAbort.signal : undefined
        }).then(function(resp) {
            if (!resp.ok) {
                return resp.json().then(function(d) { throw {data: d}; });
            }
            var reader = resp.body.getReader();
            var decoder = new TextDecoder();

            function read() {
                return reader.read().then(function(result) {
                    _bumpAiActivity(); // stream is alive — reset the idle-abort + reaper deadline
                    if (result.done) { finish(); return; }
                    var chunk = decoder.decode(result.value, {stream: true});
                    var lines = chunk.split('\n');
                    for (var i = 0; i < lines.length; i++) {
                        if (lines[i].indexOf('data: ') !== 0) continue;
                        try {
                            var d = JSON.parse(lines[i].substring(6));
                            if (d.ctx) {
                                contextSummary = d.ctx;
                                _aiLastContextSummary = d.ctx;
                                _updateContextBar();
                                _updateContextChip();
                            }
                            if (d.t) {
                                // Remove typing indicator on first chunk
                                var _typing = document.getElementById('_ai-typing');
                                if (_typing) { _typing.remove(); aiDiv.style.display = ''; }
                                fullText += d.t;
                                contentEl.innerHTML = _aiRenderMarkdown(fullText);
                                chatEl.scrollTop = chatEl.scrollHeight;
                            }
                            // Tool call indicator (read tool auto-executing)
                            if (d.tool_call && d.tool_call.name) {
                                var _typing2 = document.getElementById('_ai-typing');
                                if (_typing2) _typing2.remove();
                                var tcEl = document.createElement('div');
                                tcEl.className = 'ai-tool-indicator';
                                tcEl.innerHTML = '<span class="ai-tool-spinner"></span> Fetching ' + d.tool_call.name.replace(/_/g, ' ').replace(/^get /, '') + '...';
                                tcEl.id = '_ai-tool-' + Date.now();
                                aiDiv.appendChild(tcEl);
                                chatEl.scrollTop = chatEl.scrollHeight;
                                // Mark for resolution when next text arrives
                                aiDiv._lastToolIndicator = tcEl;
                            }
                            // Resolve tool indicator when text resumes
                            if (d.t && aiDiv._lastToolIndicator) {
                                aiDiv._lastToolIndicator.innerHTML = '\u2713 ' + aiDiv._lastToolIndicator.textContent.replace('...', '').replace(/^[\u25cb\u23f3] /, '');
                                aiDiv._lastToolIndicator.classList.add('ai-tool-done');
                                aiDiv._lastToolIndicator = null;
                            }
                            // Write tool proposed (needs user confirmation)
                            if (d.action && d.action.tool) {
                                var _typing3 = document.getElementById('_ai-typing');
                                if (_typing3) _typing3.remove();
                                var actionCard = document.createElement('div');
                                actionCard.className = 'ai-action-card';
                                var toolLabel = d.action.tool.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
                                var argSummary = '';
                                var args = d.action.arguments || {};
                                if (args.symbol || args.asset) argSummary += (args.symbol || args.asset);
                                if (args.price) argSummary += (argSummary ? ' ' : '') + 'at $' + args.price;
                                if (args.condition) argSummary += ' ' + args.condition;
                                if (args.title) argSummary += (argSummary ? ' — ' : '') + args.title;
                                if (args.direction) argSummary += (argSummary ? ' ' : '') + '(' + args.direction + ')';
                                actionCard.innerHTML = '<div class="ai-action-title">' + toolLabel + '</div>' +
                                    (argSummary ? '<div class="ai-action-summary">' + argSummary + '</div>' : '') +
                                    '<div class="ai-action-btns">' +
                                    '<button class="ai-action-confirm" data-action-id="' + escapeHtml(d.action.id || '') + '" data-tool="' + escapeHtml(d.action.tool) + '" data-args=\'' + JSON.stringify(d.action.arguments || {}).replace(/'/g, '&#39;') + '\'>\u2713 Confirm</button>' +
                                    '<button class="ai-action-cancel">\u2717 Cancel</button>' +
                                    '</div>';
                                aiDiv.appendChild(actionCard);
                                chatEl.scrollTop = chatEl.scrollHeight;
                                // Confirm handler
                                actionCard.querySelector('.ai-action-confirm').addEventListener('click', function() {
                                    var btn = this;
                                    btn.disabled = true;
                                    btn.textContent = 'Executing...';
                                    var csrf = document.querySelector('meta[name="csrf-token"]');
                                    fetch('/api/ai/execute-action', {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf ? csrf.content : ''},
                                        body: JSON.stringify({tool: btn.getAttribute('data-tool'), arguments: JSON.parse(btn.getAttribute('data-args'))}),
                                    }).then(function(r) { return r.json(); }).then(function(res) {
                                        if (res.error) {
                                            btn.textContent = '\u2717 Failed: ' + res.error;
                                            btn.classList.add('ai-action-failed');
                                        } else {
                                            btn.textContent = '\u2713 Done';
                                            btn.classList.add('ai-action-success');
                                            actionCard.querySelector('.ai-action-cancel').style.display = 'none';
                                        }
                                    }).catch(function() {
                                        btn.textContent = '\u2717 Failed';
                                        btn.classList.add('ai-action-failed');
                                    });
                                });
                                // Cancel handler
                                actionCard.querySelector('.ai-action-cancel').addEventListener('click', function() {
                                    actionCard.querySelector('.ai-action-confirm').disabled = true;
                                    this.textContent = 'Cancelled';
                                    this.disabled = true;
                                });
                            }
                            if (d.suggestions && Array.isArray(d.suggestions) && d.suggestions.length > 0) {
                                var pillsDiv = document.createElement('div');
                                pillsDiv.className = 'ai-followup-pills';
                                d.suggestions.forEach(function(s) {
                                    var pill = document.createElement('button');
                                    pill.className = 'ai-followup-pill';
                                    pill.textContent = s;
                                    pill.addEventListener('click', function() {
                                        pillsDiv.remove();
                                        _aiPanelDoSend(s, 'chat');
                                    });
                                    pillsDiv.appendChild(pill);
                                });
                                aiDiv.appendChild(pillsDiv);
                                chatEl.scrollTop = chatEl.scrollHeight;
                            }
                            if (d.done) {
                                if (d.conversation_id) _aiSetConversationId(d.conversation_id);
                                if (d.bal != null) _updateAIPanelBal(d.bal);
                                var footer = document.createElement('div');
                                footer.className = 'ai-msg-footer';
                                if (d.charged) {
                                    var costSpan = document.createElement('span');
                                    costSpan.className = 'ai-panel-cost';
                                    costSpan.textContent = '$' + (d.charged / 100).toFixed(2);
                                    footer.appendChild(costSpan);
                                }
                                if (contextSummary) {
                                    var ctxBtn = document.createElement('button');
                                    ctxBtn.className = 'ai-ctx-toggle';
                                    ctxBtn.textContent = 'Context';
                                    ctxBtn.onclick = function() {
                                        var box = this.nextElementSibling;
                                        if (box) box.style.display = box.style.display === 'none' ? 'block' : 'none';
                                    };
                                    footer.appendChild(ctxBtn);
                                    var ctxBox = document.createElement('pre');
                                    ctxBox.className = 'ai-ctx-box';
                                    ctxBox.style.display = 'none';
                                    ctxBox.textContent = contextSummary;
                                    footer.appendChild(ctxBox);
                                }
                                aiDiv.appendChild(footer);
                            }
                            if (d.error) {
                                _aiPanelError(d.error);
                                if (d.bal != null) _updateAIPanelBal(d.bal);
                            }
                        } catch(ex) {
                            if (typeof console !== 'undefined') console.warn('AI SSE parse error:', ex, lines[i]);
                        }
                    }
                    return read();
                });
            }
            return read();
        }).catch(function(err) {
            var msg = (err && err.name === 'AbortError') ? 'Response timed out — please try again' : 'Network error';
            if (err && err.data) {
                msg = err.data.error || msg;
                if (err.data.upgrade_required && typeof showUpgradeModal === 'function') {
                    showUpgradeModal(msg);
                    finish();
                    return;
                }
                if (err.data.need_credits) {
                    _aiPanelError(msg + '');
                    finish();
                    return;
                }
            }
            _aiPanelError(msg);
            finish();
        });
    };

    // Auto-create conversation on first message (with metadata)
    if (!_aiConversationId) {
        _aiConversationId = -1; // sentinel to prevent duplicate creation
        fetch('/api/ai/conversations', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
            credentials: 'same-origin',
            body: JSON.stringify({primary_asset: asset, timeframe: tf, page_context: pageCtx}),
        }).then(function(r) { return r.json(); })
        .then(function(data) {
            _aiSetConversationId(data.id);
            sendWithConv(_aiConversationId);
        }).catch(function() {
            _aiConversationId = null; // reset sentinel on failure
            sendWithConv(null);
        });
    } else if (_aiConversationId === -1) {
        // creation already in flight — clean up streaming state
        finish();
        return;
    } else {
        sendWithConv(_aiConversationId);
    }

    function finish() {
        if (_aiIdleTimer) { clearTimeout(_aiIdleTimer); _aiIdleTimer = null; }
        aiPanelStreaming = false;
        var _typingLeft = document.getElementById('_ai-typing');
        if (_typingLeft) _typingLeft.remove(); // never let the indicator outlive the stream
        _setAIPanelSendState(true);
        refreshAIPanelBalance();
    }
}

function _aiPanelError(msg) {
    var chatEl = document.getElementById('ai-panel-chat');
    if (!chatEl) return;
    var errDiv = document.createElement('div');
    errDiv.className = 'ai-panel-msg ai-panel-msg-error';
    errDiv.textContent = msg;
    chatEl.appendChild(errDiv);
    chatEl.scrollTop = chatEl.scrollHeight;
}

function _setAIPanelSendState(enabled) {
    var btn = document.getElementById('ai-panel-send');
    if (btn) btn.disabled = !enabled;
    var ta = document.getElementById('ai-panel-textarea');
    if (ta) ta.disabled = !enabled;
}

function refreshAIPanelBalance() {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    fetch('/api/ai/balance', {headers: {'X-CSRFToken': csrf}})
    .then(function(r) { return r.json(); })
    .then(function(d) {
        _updateAIPanelBal(d.balance_cents || 0);
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/ai/balance failed — rendered as empty', String((e && e.message) || e)); });
}

function _updateAIPanelBal(cents) {
    aiPanelBalance = cents;
    var el = document.getElementById('ai-panel-bal');
    if (!el) return;
    el.textContent = '$' + (cents / 100).toFixed(2);
    el.className = 'ai-panel-bal' + (cents === 0 ? ' zero' : cents < 100 ? ' low' : '');
}

// ============================================================
// Journal Tab
// ============================================================

var _journalLastAsset = null;

function refreshJournal() {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    fetch('/api/ai/journal?days=30', {headers: {'X-CSRFToken': csrf}})
    .then(function(r) { return r.json(); })
    .then(function(data) {
        _renderJournalStats(data.stats || {});
        _renderOpenTrades(data.open || []);
        _renderClosedTrades(data.history || []);
        _journalOpenCache = data.open || [];
        _renderLivePnl();
        _renderJournalOnboarding((data.open || []).length + (data.history || []).length);
    }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/ai/journal failed — rendered as empty', String((e && e.message) || e)); });

    _refreshJournalAssetTrades();
}

/**
 * First-run states for the sidebar journal.
 * 0 trades: swap the (empty) dashboard card for an onboarding block.
 * 1-4 trades: show a slim milestone nudge above the tab bar.
 */
function _renderJournalOnboarding(total) {
    var onboarding = document.getElementById('jp-onboarding');
    var dashboard = document.getElementById('jp-dashboard');
    var nudge = document.getElementById('jp-milestone-nudge');
    if (!onboarding || !dashboard) return;
    // The regular fetch is a 30-day window — before treating "no recent
    // trades" as a brand-new user, check lifetime history once so a returning
    // trader keeps the dashboard instead of getting "log your first trade".
    if (total === 0 && window._journalLifetimeTotal === undefined) {
        window._journalLifetimeTotal = null; // in flight
        var csrf = '';
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) csrf = meta.content;
        fetch('/api/ai/journal?days=3650', {headers: {'X-CSRFToken': csrf}})
            .then(function(r) { return r.json(); })
            .then(function(d) {
                window._journalLifetimeTotal = (d.open || []).length + (d.history || []).length;
                _renderJournalOnboarding(0);
            })
            .catch(function() { window._journalLifetimeTotal = undefined; });
        return; // leave the default (dashboard) visible while checking
    }
    if (total === 0 && window._journalLifetimeTotal === null) return; // still in flight
    if (total === 0) total = window._journalLifetimeTotal || 0;
    var isNew = total === 0;
    var wasHidden = dashboard.style.display === 'none';
    onboarding.style.display = isNew ? '' : 'none';
    dashboard.style.display = isNew ? 'none' : '';
    // Dashboard data is fetched once at init; re-fetch when the card first
    // reappears after the zero-trades state (first trade just logged).
    if (!isNew && wasHidden && typeof _loadDashboardData === 'function') {
        try { _loadDashboardData(); } catch (e) {}
    }
    if (nudge) {
        if (total >= 1 && total <= 4) {
            nudge.textContent = total === 1
                ? 'First trade logged. Close it out to see your R-multiple and AI grade.'
                : total + ' trades logged — keep going. Streak and win-rate tracking sharpen with more history.';
            nudge.style.display = '';
        } else {
            nudge.style.display = 'none';
        }
    }
}

function _refreshJournalAssetTrades() {
    var asset = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || '';
    var section = document.getElementById('journal-asset-section');
    var label = document.getElementById('journal-asset-label');
    var el = document.getElementById('journal-asset-trades');
    if (!section || !el) return;

    if (!asset) {
        section.style.display = 'none';
        return;
    }

    if (asset === _journalLastAsset) return;
    _journalLastAsset = asset;

    label.textContent = asset;
    el.innerHTML = '<div class="journal-empty">Loading...</div>';
    section.style.display = '';

    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    fetch('/api/ai/journal/asset-history?asset=' + encodeURIComponent(asset) + '&days=365&limit=10', {
        headers: {'X-CSRFToken': csrf}
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var trades = data.trades || [];
        if (!trades.length) {
            el.innerHTML = '<div class="journal-empty">No trades on ' + asset + '</div>';
            return;
        }
        el.innerHTML = '';
        trades.forEach(function(t) {
            var isOpen = !t.closed_at;
            var card = _createTradeCard(t, isOpen);
            el.appendChild(card);
        });
    }).catch(function() {
        el.innerHTML = '<div class="journal-empty">Failed to load</div>';
    });
}

function _renderJournalStats(stats) {
    _journalStatsCache = stats;
    // Stats now render in the dashboard live P&L area — keep header clean
    var el = document.getElementById('journal-stats-bar');
    if (el) el.innerHTML = '';
    _renderLivePnl();
}

var _journalOpenAll = [];
function _renderOpenTrades(trades) {
    var el = document.getElementById('journal-open-trades');
    var countEl = document.getElementById('jp-open-count');
    var titleEl = el ? el.closest('.jp-section') : null;
    var titleDiv = titleEl ? titleEl.querySelector('.jp-section-title') : null;
    if (!el) return;

    // Filter out current chart asset (already shown in "this chart" section)
    var currentAsset = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || '';
    var filtered = currentAsset ? trades.filter(function(t) { return t.asset !== currentAsset; }) : trades;
    _journalOpenAll = filtered;

    // Update title text + count without destroying the Watch toggle
    if (titleDiv) {
        var label = currentAsset ? 'Other Open' : 'Open Positions';
        // Find or create the text span for the label
        var labelSpan = titleDiv.querySelector('.jp-open-label');
        if (!labelSpan) {
            labelSpan = document.createElement('span');
            labelSpan.className = 'jp-open-label';
            titleDiv.insertBefore(labelSpan, titleDiv.firstChild);
        }
        var countHtml = filtered.length ? ' <span class="jp-count">(' + filtered.length + ')</span>' : '';
        labelSpan.innerHTML = label + countHtml;
    }

    if (!filtered.length) {
        el.innerHTML = '<div class="journal-empty">' + (currentAsset ? 'No other open trades' : 'No open trades') + '</div>';
        return;
    }
    el.innerHTML = '';
    var show = Math.min(5, filtered.length);
    for (var i = 0; i < show; i++) {
        el.appendChild(_createTradeCard(filtered[i], true));
    }
    if (filtered.length > 5) {
        var more = document.createElement('button');
        more.className = 'jp-show-more';
        more.textContent = 'Show all ' + filtered.length + ' positions \u2192';
        more.addEventListener('click', function() {
            el.innerHTML = '';
            _journalOpenAll.forEach(function(t) {
                el.appendChild(_createTradeCard(t, true));
            });
        });
        el.appendChild(more);
    }
}

var _journalClosedAll = [];
function _renderClosedTrades(trades) {
    var el = document.getElementById('journal-closed-trades');
    var countEl = document.getElementById('jp-closed-count');
    if (!el) return;
    _journalClosedAll = trades;
    if (countEl) countEl.textContent = trades.length ? '(' + trades.length + ')' : '';
    if (!trades.length) {
        el.innerHTML = '<div class="journal-empty">No closed trades</div>';
        return;
    }
    el.innerHTML = '';
    var show = Math.min(5, trades.length);
    for (var i = 0; i < show; i++) {
        el.appendChild(_createTradeCard(trades[i], false));
    }
    if (trades.length > 5) {
        var more = document.createElement('button');
        more.className = 'jp-show-more';
        more.textContent = 'Show all ' + trades.length + ' trades \u2192';
        more.addEventListener('click', function() {
            el.innerHTML = '';
            _journalClosedAll.forEach(function(t) {
                el.appendChild(_createTradeCard(t, false));
            });
        });
        el.appendChild(more);
    }
}

function _createTradeCard(trade, isOpen) {
    var card = document.createElement('div');
    card.className = 'journal-trade-card';
    if (isOpen) card.classList.add('jc-open');
    else if (trade.rr_actual > 0) card.classList.add('jc-win');
    else if (trade.rr_actual != null && trade.rr_actual < 0) card.classList.add('jc-loss');
    card.setAttribute('data-journal-id', trade.id);

    var dirLabel = trade.direction === 'long' ? 'LONG' : 'SHORT';
    var dirCls = trade.direction === 'long' ? 'jc-dir-long' : 'jc-dir-short';

    // ── Row 1: [DIR badge] [ASSET] ... [P&L] [close btn] ──
    var row1 = document.createElement('div');
    row1.className = 'journal-trade-header';

    var left = document.createElement('span');
    left.className = 'jc-left';
    left.innerHTML = '<span class="jc-dir-badge ' + dirCls + '">' + dirLabel + '</span> <strong>' + escapeHtml(trade.asset) + '</strong>';

    var right = document.createElement('span');
    right.className = 'journal-trade-meta';

    if (isOpen && trade.entry_price) {
        var curPrice = null;
        try {
            if (typeof klineChart !== 'undefined' && klineChart && trade.asset === klineChartSymbol) {
                var dl = klineChart.getDataList();
                if (dl && dl.length) curPrice = dl[dl.length - 1].close;
            }
        } catch(e) {}
        if (curPrice && curPrice > 0) {
            var pctPnl = trade.direction === 'short'
                ? ((trade.entry_price - curPrice) / trade.entry_price * 100)
                : ((curPrice - trade.entry_price) / trade.entry_price * 100);
            var dollarPnl = trade.size ? pctPnl / 100 * trade.size : null;
            var pnlColor = pctPnl >= 0 ? '#26a69a' : '#ef5350';
            var pctStr = (pctPnl >= 0 ? '+' : '') + pctPnl.toFixed(1) + '%';
            var dolStr = dollarPnl != null ? ' ' + (dollarPnl >= 0 ? '+$' : '-$') + Math.abs(dollarPnl).toFixed(0) : '';
            right.innerHTML = '<span style="color:' + pnlColor + '">' + pctStr + dolStr + '</span>';
        } else {
            right.innerHTML = '<span style="color:#484f58;">' + _formatPrice(trade.entry_price) + '</span>';
        }
    } else if (!isOpen) {
        var pnlHtml = '';
        if (trade.entry_price && trade.exit_price) {
            var pctPnl = trade.direction === 'short'
                ? ((trade.entry_price - trade.exit_price) / trade.entry_price * 100)
                : ((trade.exit_price - trade.entry_price) / trade.entry_price * 100);
            var dollarPnl = trade.pnl_currency;
            if (dollarPnl == null && trade.size) dollarPnl = pctPnl / 100 * trade.size;
            var pnlColor = pctPnl >= 0 ? '#26a69a' : '#ef5350';
            var pctStr = (pctPnl >= 0 ? '+' : '') + pctPnl.toFixed(1) + '%';
            var dolStr = dollarPnl != null ? ' ' + (dollarPnl >= 0 ? '+$' : '-$') + Math.abs(dollarPnl).toFixed(0) : '';
            pnlHtml = '<span style="color:' + pnlColor + '">' + pctStr + dolStr + '</span>';
        }
        right.innerHTML = pnlHtml;
    }

    row1.appendChild(left);
    row1.appendChild(right);

    // Close button for open trades
    if (isOpen) {
        var closeBtn = document.createElement('button');
        closeBtn.className = 'journal-close-btn';
        closeBtn.textContent = '\u00d7';
        closeBtn.title = 'Close trade';
        closeBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            _journalShowCloseForm(trade, card);
        });
        row1.appendChild(closeBtn);
    }

    card.appendChild(row1);

    // ── Row 2: [R result] [setup tag] [grade] [mistakes] [entry score] [timeframe] [date] ──
    var row2Parts = [];

    if (!isOpen && trade.rr_actual != null) {
        var rr = (trade.rr_actual > 0 ? '+' : '') + trade.rr_actual + 'R';
        var rrColor = trade.rr_actual > 0 ? '#26a69a' : (trade.rr_actual < 0 ? '#ef5350' : '#8b949e');
        row2Parts.push('<span class="jc-r-result" style="color:' + rrColor + '">' + rr + '</span>');
    } else if (isOpen && trade.rr_planned != null) {
        row2Parts.push('<span style="color:#484f58;">' + trade.rr_planned + 'R plan</span>');
    }

    if (trade.setup_type) {
        row2Parts.push('<span class="jc-setup-tag">' + escapeHtml(trade.setup_type) + '</span>');
    }

    // AI grade badge — average of setup/execution/exit/discipline (each 1-10). Only
    // shown if a review was already generated; never triggers the paid AI call itself.
    if (trade.trade_grade) {
        try {
            var grade = typeof trade.trade_grade === 'string' ? JSON.parse(trade.trade_grade) : trade.trade_grade;
            var gradeVals = [grade.setup, grade.execution, grade.exit, grade.discipline].filter(function(v) { return typeof v === 'number'; });
            if (gradeVals.length) {
                var gradeAvg = gradeVals.reduce(function(a, b) { return a + b; }, 0) / gradeVals.length;
                var gradeColor = gradeAvg >= 7 ? '#26a69a' : (gradeAvg >= 4 ? '#d29922' : '#ef5350');
                var gradeTitle = 'AI grade — setup ' + (grade.setup != null ? grade.setup : '-') +
                    ', execution ' + (grade.execution != null ? grade.execution : '-') +
                    ', exit ' + (grade.exit != null ? grade.exit : '-') +
                    ', discipline ' + (grade.discipline != null ? grade.discipline : '-');
                row2Parts.push('<span title="' + escapeHtml(gradeTitle) + '" style="background:rgba(255,255,255,0.06);color:' + gradeColor +
                    ';padding:0 4px;border-radius:2px;border:1px solid ' + gradeColor + '40;">' + gradeAvg.toFixed(1) + '/10</span>');
            }
        } catch (e) {}
    }

    // Mistake tags — already stored server-side, display-only here
    if (trade.mistake_tags) {
        try {
            var mistakeList = typeof trade.mistake_tags === 'string' ? JSON.parse(trade.mistake_tags) : trade.mistake_tags;
            if (Array.isArray(mistakeList)) {
                mistakeList.forEach(function(tag) {
                    row2Parts.push('<span style="background:rgba(245,158,11,0.12);color:#f59e0b;padding:0 4px;border-radius:2px;border:1px solid rgba(245,158,11,0.3);">' + escapeHtml(tag) + '</span>');
                });
            }
        } catch (e) {}
    }

    // Score-at-entry chip — only present for scanner-sourced trades
    if (trade.indicators_at_entry) {
        try {
            var entryInd = typeof trade.indicators_at_entry === 'string' ? JSON.parse(trade.indicators_at_entry) : trade.indicators_at_entry;
            var entryParts = [];
            if (entryInd.score_total != null) entryParts.push('Score ' + entryInd.score_total);
            if (entryInd.verdict) entryParts.push(entryInd.verdict);
            if (entryParts.length) {
                row2Parts.push('<span style="color:#787b86;">Entry: ' + escapeHtml(entryParts.join(' · ')) + '</span>');
            }
        } catch (e) {}
    }

    if (trade.timeframe) {
        row2Parts.push('<span style="color:#30363d;">' + escapeHtml(trade.timeframe) + '</span>');
    }

    var dateStr = '';
    if (!isOpen && trade.closed_at) dateStr = trade.closed_at.substring(5, 10);
    else if (isOpen && trade.opened_at) dateStr = trade.opened_at.substring(5, 10);
    if (dateStr) row2Parts.push('<span style="color:#30363d;">' + dateStr + '</span>');

    if (row2Parts.length > 0) {
        var row2 = document.createElement('div');
        row2.className = 'jc-row-2';
        row2.innerHTML = row2Parts.join(' ');
        card.appendChild(row2);
    }

    // Click to expand edit form
    card.addEventListener('click', function(e) {
        if (e.target.closest('.journal-edit-form') || e.target.closest('.journal-close-form') || e.target.closest('button')) return;
        var existing = card.querySelector('.journal-edit-form');
        if (existing) {
            existing.remove();
            return;
        }
        var form = _createEditForm(trade, isOpen);
        card.appendChild(form);
    });

    return card;
}

function _createEditForm(trade, isOpen) {
    var form = document.createElement('div');
    form.className = 'journal-edit-form';
    form.innerHTML =
        '<div class="journal-form-row">' +
            '<label>Entry</label><input type="number" step="any" class="jf-entry" value="' + escapeHtml(trade.entry_price || '') + '">' +
            '<label>SL</label><input type="number" step="any" class="jf-stop" value="' + escapeHtml(trade.stop_price || '') + '">' +
            '<label>TP</label><input type="number" step="any" class="jf-target" value="' + escapeHtml(trade.target_price || '') + '">' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<label>Dir</label><select class="jf-direction"><option value="long"' + (trade.direction === 'long' ? ' selected' : '') + '>Long</option><option value="short"' + (trade.direction === 'short' ? ' selected' : '') + '>Short</option></select>' +
            '<label>Setup</label><select class="jf-setup">' +
                '<option value="">--</option>' +
                '<option value="breakout"' + (trade.setup_type === 'breakout' ? ' selected' : '') + '>Breakout</option>' +
                '<option value="pullback"' + (trade.setup_type === 'pullback' ? ' selected' : '') + '>Pullback</option>' +
                '<option value="reversal"' + (trade.setup_type === 'reversal' ? ' selected' : '') + '>Reversal</option>' +
                '<option value="range"' + (trade.setup_type === 'range' ? ' selected' : '') + '>Range</option>' +
                '<option value="momentum"' + (trade.setup_type === 'momentum' ? ' selected' : '') + '>Momentum</option>' +
                '<option value="other"' + (trade.setup_type === 'other' ? ' selected' : '') + '>Other</option>' +
            '</select>' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<textarea class="jf-notes" placeholder="Notes..." rows="2">' + escapeHtml(trade.notes || '') + '</textarea>' +
        '</div>' +
        (trade.closed_at ? (
            '<div class="journal-form-row" style="flex-direction:column;align-items:stretch;">' +
            (trade.ai_review ? '<div style="font-size:0.68rem;line-height:1.5;color:#8b949e;max-height:150px;overflow-y:auto;margin-bottom:6px;">' + _journalRenderMarkdown(trade.ai_review) + '</div>' : '') +
            '<button class="journal-action-btn jf-ai-review-btn" type="button">' + (trade.ai_review ? 'Regenerate AI Review' : 'Generate AI Review') + '</button>' +
            '</div>'
        ) : '') +
        '<div class="journal-form-row">' +
            '<button class="journal-action-btn jf-save">Save</button>' +
            '<button class="journal-action-btn jf-cancel">Cancel</button>' +
            (!isOpen && trade.closed_at ? '<button class="journal-action-btn jf-reopen">Reopen</button>' : '') +
        '</div>';

    // Stop propagation on form inputs
    form.addEventListener('click', function(e) { e.stopPropagation(); });
    form.addEventListener('keydown', function(e) { e.stopPropagation(); });

    // Wire buttons
    form.querySelector('.jf-save').addEventListener('click', function() {
        _journalSaveEdit(trade.id, form);
    });
    form.querySelector('.jf-cancel').addEventListener('click', function() {
        form.remove();
    });
    var reopenBtn = form.querySelector('.jf-reopen');
    if (reopenBtn) {
        reopenBtn.addEventListener('click', function() {
            _journalReopen(trade.id);
        });
    }
    var aiReviewBtn = form.querySelector('.jf-ai-review-btn');
    if (aiReviewBtn) {
        aiReviewBtn.addEventListener('click', function() {
            _journalRequestAiReview(trade.id, aiReviewBtn);
        });
    }

    return form;
}

// Lightweight markdown → HTML for the sidebar AI review (bold, bullets, line
// breaks) — mirrors the full /journal page's renderMarkdown so the review
// reads formatted instead of raw asterisks. Input is escaped first.
function _journalRenderMarkdown(text) {
    if (!text) return '';
    return escapeHtml(text)
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/^- (.+)/gm, '<div style="padding-left:12px">• $1</div>')
        .replace(/\n/g, '<br>');
}

// Paid, on-demand AI trade review — only ever triggered by explicit button click,
// never auto-run on card expand. Same endpoint as the full /journal page's
// "Generate AI Review" (static/js/journal-page.js requestAIReview).
function _journalRequestAiReview(tradeId, btn) {
    var prevLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    fetch('/api/journal/trades/' + tradeId + '/ai-review', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
    }).then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.error) {
            if (btn) { btn.disabled = false; btn.textContent = prevLabel; }
            return;
        }
        refreshJournal();
    }).catch(function() {
        if (btn) { btn.disabled = false; btn.textContent = prevLabel; }
    });
}

function _journalSaveEdit(journalId, form) {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    var data = {
        entry_price: parseFloat(form.querySelector('.jf-entry').value) || undefined,
        stop_price: parseFloat(form.querySelector('.jf-stop').value) || undefined,
        target_price: parseFloat(form.querySelector('.jf-target').value) || undefined,
        direction: form.querySelector('.jf-direction').value,
        setup_type: form.querySelector('.jf-setup').value || undefined,
        notes: form.querySelector('.jf-notes').value || undefined,
    };

    fetch('/api/ai/journal/' + journalId, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify(data)
    }).then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.ok) refreshJournal();
    }).catch(function(e) {
        if (typeof window.carracLog === 'function') window.carracLog('journal', 'journal edit save failed', String((e && e.message) || e));
        if (typeof showToast === 'function') showToast('Journal edit could not be saved — check your connection and try again');
    });
}

function _journalReopen(journalId) {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    fetch('/api/ai/journal/' + journalId + '/reopen', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
    }).then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.ok) refreshJournal();
    }).catch(function(e) {
        if (typeof window.carracLog === 'function') window.carracLog('journal', 'journal reopen failed', String((e && e.message) || e));
        if (typeof showToast === 'function') showToast('Could not reopen trade — check your connection and try again');
    });
}

function _journalShowCloseForm(trade, card) {
    // Remove any existing close form
    var existing = card.querySelector('.journal-close-form');
    if (existing) { existing.remove(); return; }

    // Pre-fill exit price from current chart price if same asset
    var prefillPrice = '';
    try {
        if (typeof klineChart !== 'undefined' && klineChart && trade.asset === klineChartSymbol) {
            var dl = klineChart.getDataList();
            if (dl && dl.length) prefillPrice = dl[dl.length - 1].close;
        }
    } catch(e) {}

    var form = document.createElement('div');
    form.className = 'journal-close-form';
    form.innerHTML =
        '<div class="journal-form-row">' +
            '<label>Exit $</label><input type="number" step="any" class="jf-exit-price" placeholder="Exit price"' + (prefillPrice ? ' value="' + escapeHtml(prefillPrice) + '"' : '') + '>' +
            '<label>Reason</label><select class="jf-exit-reason">' +
                '<option value="manual">Manual</option>' +
                '<option value="tp_hit">TP Hit</option>' +
                '<option value="sl_hit">SL Hit</option>' +
                '<option value="breakeven">Breakeven</option>' +
                '<option value="time_stop">Time Stop</option>' +
            '</select>' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<textarea class="jf-exit-notes" placeholder="Close notes..." rows="1"></textarea>' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<button class="journal-action-btn jf-confirm-close">Confirm Close</button>' +
        '</div>';

    form.addEventListener('click', function(e) { e.stopPropagation(); });
    form.addEventListener('keydown', function(e) { e.stopPropagation(); });

    form.querySelector('.jf-confirm-close').addEventListener('click', function() {
        var exitPrice = parseFloat(form.querySelector('.jf-exit-price').value);
        if (!exitPrice) return;
        var csrf = '';
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) csrf = meta.content;

        fetch('/api/ai/journal/close', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
            body: JSON.stringify({
                id: trade.id,
                exit_price: exitPrice,
                exit_reason: form.querySelector('.jf-exit-reason').value,
                notes: form.querySelector('.jf-exit-notes').value || undefined,
            })
        }).then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.ok) refreshJournal();
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('journal', 'journal close failed', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Trade close could not be saved — check your connection and try again');
        });
    });

    card.appendChild(form);
}

function _journalShowLogForm() {
    var container = document.getElementById('journal-open-trades');
    if (!container) return;

    // Remove existing form
    var existing = container.querySelector('.journal-log-form');
    if (existing) { existing.remove(); return; }

    var asset = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || '';
    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';

    var form = document.createElement('div');
    form.className = 'journal-log-form journal-edit-form';
    form.innerHTML =
        '<div class="journal-section-title" style="margin-bottom:4px;">Log Manual Trade</div>' +
        '<div class="journal-form-row">' +
            '<label>Asset</label><input type="text" class="jf-asset" value="' + escapeHtml(asset) + '" style="width:60px;text-transform:uppercase;">' +
            '<label>TF</label><input type="text" class="jf-tf" value="' + escapeHtml(tf) + '" style="width:40px;">' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<label>Entry</label><input type="number" step="any" class="jf-entry">' +
            '<label>SL</label><input type="number" step="any" class="jf-stop">' +
            '<label>TP</label><input type="number" step="any" class="jf-target">' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<label>Dir</label><select class="jf-direction"><option value="long">Long</option><option value="short">Short</option></select>' +
            '<label>Setup</label><select class="jf-setup">' +
                '<option value="">--</option>' +
                '<option value="breakout">Breakout</option><option value="pullback">Pullback</option>' +
                '<option value="reversal">Reversal</option><option value="range">Range</option>' +
                '<option value="momentum">Momentum</option><option value="other">Other</option>' +
            '</select>' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<textarea class="jf-notes" placeholder="Notes..." rows="1"></textarea>' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<button class="journal-action-btn jf-log-submit">Open Trade</button>' +
            '<button class="journal-action-btn jf-cancel">Cancel</button>' +
        '</div>';

    form.addEventListener('click', function(e) { e.stopPropagation(); });
    form.addEventListener('keydown', function(e) { e.stopPropagation(); });

    form.querySelector('.jf-log-submit').addEventListener('click', function() {
        var csrf = '';
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) csrf = meta.content;

        var entryPrice = parseFloat(form.querySelector('.jf-entry').value);
        var assetVal = form.querySelector('.jf-asset').value.toUpperCase().trim();
        if (!entryPrice || !assetVal) return;

        fetch('/api/ai/journal/open', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
            body: JSON.stringify({
                asset: assetVal,
                timeframe: form.querySelector('.jf-tf').value || '1D',
                direction: form.querySelector('.jf-direction').value,
                entry_price: entryPrice,
                stop_price: parseFloat(form.querySelector('.jf-stop').value) || undefined,
                target_price: parseFloat(form.querySelector('.jf-target').value) || undefined,
                setup_type: form.querySelector('.jf-setup').value || undefined,
            })
        }).then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.id) { form.remove(); refreshJournal(); }
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('journal', 'journal open (manual log) failed', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Trade could not be logged — check your connection and try again');
        });
    });

    form.querySelector('.jf-cancel').addEventListener('click', function() { form.remove(); });

    container.insertBefore(form, container.firstChild);
}

function _journalShowLogFormPrefilled(data) {
    var container = document.getElementById('journal-open-trades');
    if (!container) return;
    var existing = container.querySelector('.journal-log-form');
    if (existing) existing.remove();

    var form = document.createElement('div');
    form.className = 'journal-log-form journal-edit-form';
    var dirVal = data.direction || 'long';
    form.innerHTML =
        '<div class="journal-section-title" style="margin-bottom:4px;">Log Setup: ' + escapeHtml(data.scenarioName || '') + '</div>' +
        '<div class="journal-form-row">' +
            '<label>Asset</label><input type="text" class="jf-asset" value="' + escapeHtml(data.asset || '') + '" style="width:60px;text-transform:uppercase;">' +
            '<label>TF</label><input type="text" class="jf-tf" value="' + escapeHtml(data.timeframe || '1D') + '" style="width:40px;">' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<label>Entry</label><input type="number" step="any" class="jf-entry"' + (data.entry ? ' value="' + escapeHtml(data.entry) + '"' : '') + '>' +
            '<label>SL</label><input type="number" step="any" class="jf-stop"' + (data.stop ? ' value="' + escapeHtml(data.stop) + '"' : '') + '>' +
            '<label>TP</label><input type="number" step="any" class="jf-target"' + (data.target ? ' value="' + escapeHtml(data.target) + '"' : '') + '>' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<label>Dir</label><select class="jf-direction"><option value="long"' + (dirVal === 'long' ? ' selected' : '') + '>Long</option><option value="short"' + (dirVal === 'short' ? ' selected' : '') + '>Short</option></select>' +
            '<label>Setup</label><select class="jf-setup">' +
                '<option value="">--</option>' +
                '<option value="breakout">Breakout</option><option value="pullback">Pullback</option>' +
                '<option value="reversal">Reversal</option><option value="range">Range</option>' +
                '<option value="momentum">Momentum</option><option value="other">Other</option>' +
            '</select>' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<textarea class="jf-notes" placeholder="Notes..." rows="1"></textarea>' +
        '</div>' +
        '<div class="journal-form-row">' +
            '<button class="journal-action-btn jf-log-submit">Open Trade</button>' +
            '<button class="journal-action-btn jf-cancel">Cancel</button>' +
        '</div>';

    form.addEventListener('click', function(e) { e.stopPropagation(); });
    form.addEventListener('keydown', function(e) { e.stopPropagation(); });

    form.querySelector('.jf-log-submit').addEventListener('click', function() {
        var csrf = '';
        var m = document.querySelector('meta[name="csrf-token"]');
        if (m) csrf = m.content;
        var entryPrice = parseFloat(form.querySelector('.jf-entry').value);
        var assetVal = form.querySelector('.jf-asset').value.toUpperCase().trim();
        if (!entryPrice || !assetVal) return;
        var body = {
            asset: assetVal,
            timeframe: form.querySelector('.jf-tf').value || '1D',
            direction: form.querySelector('.jf-direction').value,
            entry_price: entryPrice,
            stop_price: parseFloat(form.querySelector('.jf-stop').value) || undefined,
            target_price: parseFloat(form.querySelector('.jf-target').value) || undefined,
            setup_type: form.querySelector('.jf-setup').value || undefined,
        };
        if (data.scenarioId) body.scenario_id = data.scenarioId;
        fetch('/api/ai/journal/open', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
            body: JSON.stringify(body)
        }).then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.id) { form.remove(); refreshJournal(); }
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('journal', 'journal open (prefilled scenario) failed', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Trade could not be logged — check your connection and try again');
        });
    });

    form.querySelector('.jf-cancel').addEventListener('click', function() { form.remove(); });
    container.insertBefore(form, container.firstChild);
}

function _journalImportDrawings() {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    var btn = document.getElementById('journal-import-drawings');
    if (btn) btn.disabled = true;

    fetch('/api/ai/journal/import', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
    }).then(function(r) { return r.json(); })
    .then(function(d) {
        if (btn) btn.disabled = false;
        var count = d.imported || 0;
        if (btn) btn.textContent = count > 0 ? 'Imported ' + count + ' trades' : 'No new drawings';
        setTimeout(function() { if (btn) btn.textContent = 'Import Drawings'; }, 3000);
        if (count > 0) refreshJournal();
    }).catch(function() {
        if (btn) btn.disabled = false;
    });
}

function _journalWeeklyReview() {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    var output = document.getElementById('journal-review-output');
    if (!output) return;
    output.style.display = 'block';
    output.innerHTML = '<div class="ai-msg-content">Generating review...</div>';

    var contentEl = output.querySelector('.ai-msg-content');
    var fullText = '';

    fetch('/api/ai/weekly-review', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
    }).then(function(resp) {
        if (!resp.ok) return resp.json().then(function(d) { throw {data: d}; });
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();

        function read() {
            return reader.read().then(function(result) {
                if (result.done) return;
                var chunk = decoder.decode(result.value, {stream: true});
                var lines = chunk.split('\n');
                for (var i = 0; i < lines.length; i++) {
                    if (lines[i].indexOf('data: ') !== 0) continue;
                    try {
                        var d = JSON.parse(lines[i].substring(6));
                        if (d.t) {
                            fullText += d.t;
                            contentEl.innerHTML = _aiRenderMarkdown(fullText);
                        }
                        if (d.done && d.bal != null) _updateAIPanelBal(d.bal);
                        if (d.error) contentEl.textContent = d.error;
                    } catch(ex) {}
                }
                return read();
            });
        }
        return read();
    }).catch(function(err) {
        var msg = 'Error generating review';
        if (err && err.data) {
            msg = err.data.error || msg;
            if (err.data.upgrade_required && typeof showUpgradeModal === 'function') {
                showUpgradeModal(msg);
                return;
            }
            if (err.data.need_credits) {
                contentEl.innerHTML = msg + '';
                return;
            }
        }
        contentEl.textContent = msg;
    });
}

function _formatPrice(price) {
    if (!price) return '0';
    if (price >= 1000) return price.toLocaleString(undefined, {maximumFractionDigits: 0});
    if (price >= 1) return price.toLocaleString(undefined, {maximumFractionDigits: 2});
    return price.toLocaleString(undefined, {maximumFractionDigits: 6});
}

// ============================================================
// Patterns Tab
// ============================================================

function _patternsInChat() {
    var asset = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || '';
    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';
    if (!asset) return;
    if (aiPanelStreaming) return;

    var chatEl = document.getElementById('ai-panel-chat');
    if (!chatEl) return;

    var empty = chatEl.querySelector('.ai-panel-empty');
    if (empty) empty.remove();

    // Show user "message"
    var userDiv = document.createElement('div');
    userDiv.className = 'ai-panel-msg ai-panel-msg-user';
    userDiv.textContent = 'Find similar patterns for ' + asset + ' ' + tf;
    chatEl.appendChild(userDiv);

    // Show animated loading
    var aiDiv = document.createElement('div');
    aiDiv.className = 'ai-panel-msg ai-panel-msg-ai';
    aiDiv.innerHTML = '<span class="ai-scanning-dots">Scanning patterns</span>';
    chatEl.appendChild(aiDiv);
    chatEl.scrollTop = chatEl.scrollHeight;
    aiPanelStreaming = true;
    _setAIPanelSendState(false);

    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    fetch('/api/ai/pattern-match', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify({ asset: asset, timeframe: tf })
    }).then(function(r) {
        if (!r.ok) return r.json().then(function(d) { throw {data: d}; });
        return r.json();
    }).then(function(data) {
        aiPanelStreaming = false;
        _setAIPanelSendState(true);
        if (data.error) {
            aiDiv.textContent = data.error;
            return;
        }
        if (data.balance != null) _updateAIPanelBal(data.balance);
        _patternMatchData = data;
        _renderPatternResultsInChat(data, aiDiv);
    }).catch(function(err) {
        aiPanelStreaming = false;
        _setAIPanelSendState(true);
        if (err && err.data && err.data.upgrade_required && typeof showUpgradeModal === 'function') {
            showUpgradeModal(err.data.error || 'Pattern match requires a subscription.');
            return;
        }
        if (err && err.data && err.data.need_credits) {
            aiDiv.className = 'ai-panel-msg ai-panel-msg-error';
            aiDiv.innerHTML = (err.data.error || 'Insufficient credits') + '';
            return;
        }
        aiDiv.className = 'ai-panel-msg ai-panel-msg-error';
        var msg = (err && err.data && err.data.error) || (err && err.message) || 'check your connection';
        aiDiv.textContent = 'Pattern search failed: ' + msg;
    });
}

function _renderPatternResultsInChat(data, container) {
    var matches = data.matches || [];
    if (!matches.length) {
        var msg = 'No similar patterns found';
        if (data.max_correlation) msg += ' (best match: r=' + data.max_correlation.toFixed(2) + ')';
        if (data.bars_searched) msg += ' \u2014 searched ' + data.bars_searched + ' windows';
        container.textContent = msg;
        return;
    }

    var currentPricesByTf = {};
    var pricesByTf = data.current_prices_by_tf || {};
    for (var tfKey in pricesByTf) {
        currentPricesByTf[tfKey] = pricesByTf[tfKey];
    }
    var fallbackPrices = data.current_prices || [];

    var html = '';
    matches.forEach(function(m, idx) {
        var outcomeColor = m.forward_return >= 0 ? '#26a69a' : '#ef5350';
        var returnStr = (m.forward_return >= 0 ? '+' : '') + m.forward_return.toFixed(1) + '%';
        var dateStr = m.date || m.period || '?';
        var endDateStr = m.end_date || dateStr;
        var curPrices = (m.source_tf && currentPricesByTf[m.source_tf]) ? currentPricesByTf[m.source_tf] : fallbackPrices;
        var sparkSvg = _patternSparkline(curPrices, m.prices || [], m.future_prices || []);
        var tfLabel = m.source_tf ? ' <span class="pattern-tf">' + escapeHtml(m.source_tf) + '</span>' : '';
        var sourceTf = m.source_tf || '';
        html += '<div class="pattern-match-card pattern-clickable" data-date="' + escapeHtml(dateStr) + '" data-end-date="' + escapeHtml(endDateStr) + '" data-idx="' + idx + '" data-source-tf="' + escapeHtml(sourceTf) + '">' +
            '<div class="pattern-card-info">' +
                '<span class="pattern-date">' + dateStr.substring(0, 10) + tfLabel + '</span>' +
                '<span class="pattern-corr" style="color:' + _corrColor(m.correlation || 0) + ';font-weight:600">r=' + (m.correlation || 0).toFixed(2) + '</span>' +
                '<span class="pattern-outcome" style="color:' + outcomeColor + '">\u2192 ' + returnStr + '</span>' +
            '</div>' +
            '<div class="pattern-sparkline">' + sparkSvg + '</div>' +
        '</div>';
    });

    if (data.avg_forward_return != null) {
        var avgColor = data.avg_forward_return >= 0 ? '#26a69a' : '#ef5350';
        var avgStr = (data.avg_forward_return >= 0 ? '+' : '') + data.avg_forward_return.toFixed(1) + '%';
        html += '<div class="pattern-avg">Avg outcome: <span style="color:' + avgColor + '">' + avgStr + '</span></div>';
    }

    html += '<div class="pattern-back-link" style="display:none;"><a href="#" class="pattern-back-now">\u2190 Back to current</a></div>';

    container.innerHTML = html;

    // Click handlers for pattern cards
    var cards = container.querySelectorAll('.pattern-clickable');
    for (var i = 0; i < cards.length; i++) {
        cards[i].addEventListener('click', function() {
            var dateStr = this.getAttribute('data-date');
            var endDateStr = this.getAttribute('data-end-date');
            var sourceTf = this.getAttribute('data-source-tf');
            var matchIdx = parseInt(this.getAttribute('data-idx'), 10);
            var all = container.querySelectorAll('.pattern-clickable');
            for (var j = 0; j < all.length; j++) all[j].classList.remove('pattern-active');
            this.classList.add('pattern-active');

            // Shrink panel to give chart more room
            _shrinkPanelForPattern();

            // If match is from a different TF, switch chart to that TF first
            var needsTfSwitch = sourceTf && typeof klineChartTimeframe !== 'undefined' && sourceTf !== klineChartTimeframe;
            if (needsTfSwitch) {
                klineChartTimeframe = sourceTf;
                try { sessionStorage.setItem('klineOpenTf', sourceTf); } catch(e) {}
                if (typeof loadKlineChart === 'function' && typeof klineChartSymbol !== 'undefined') {
                    loadKlineChart(klineChartSymbol, sourceTf);
                    document.querySelectorAll('.tf-selector').forEach(function(b) {
                        b.classList.toggle('active', b.getAttribute('data-tf') === sourceTf);
                    });
                }
                // Poll until chart data is loaded for the new TF, then navigate
                var _mi = matchIdx;
                var _prevLen = klineChart ? klineChart.getDataList().length : 0;
                var _pollCount = 0;
                var _poll = setInterval(function() {
                    _pollCount++;
                    if (_pollCount > 40) { clearInterval(_poll); return; } // 4s max
                    if (!klineChart) return;
                    var dl = klineChart.getDataList();
                    if (dl && dl.length > 0 && dl.length !== _prevLen) {
                        clearInterval(_poll);
                        setTimeout(function() { _navigateToPatternMatch(_mi); }, 100);
                    }
                }, 100);
            } else {
                _navigateToPatternMatch(matchIdx);
            }

            var backLink = container.querySelector('.pattern-back-link');
            if (backLink) backLink.style.display = '';
        });
    }

    // Back to now
    var backEl = container.querySelector('.pattern-back-now');
    if (backEl) {
        backEl.addEventListener('click', function(e) {
            e.preventDefault();
            _clearPatternHighlight();
            _restorePanelFromPattern();
            if (typeof klineChart !== 'undefined' && klineChart) klineChart.scrollToRealTime();
            var all = container.querySelectorAll('.pattern-clickable');
            for (var j = 0; j < all.length; j++) all[j].classList.remove('pattern-active');
            this.parentElement.style.display = 'none';
        });
    }
}

function _corrColor(r) {
    // Heat scale: 0.85 = red/orange, 0.90 = yellow, 0.95+ = green
    var t = Math.max(0, Math.min(1, (r - 0.85) / 0.15)); // 0 at 0.85, 1 at 1.0
    if (t < 0.5) {
        // red → yellow
        var s = t * 2;
        var red = 255;
        var green = Math.round(100 + 155 * s);
        return 'rgb(' + red + ',' + green + ',60)';
    } else {
        // yellow → green
        var s = (t - 0.5) * 2;
        var red = Math.round(255 * (1 - s));
        var green = Math.round(200 + 55 * s);
        return 'rgb(' + red + ',' + green + ',60)';
    }
}

function _patternSparkline(currentPrices, matchPrices, futurePrices) {
    // Use 2x internal resolution for crisp rendering on HiDPI
    var w = 360, h = 80, pad = 4;
    if (!matchPrices.length) return '';

    function normalize(arr) {
        if (!arr.length) return [];
        var min = arr[0], max = arr[0];
        for (var i = 1; i < arr.length; i++) {
            if (arr[i] < min) min = arr[i];
            if (arr[i] > max) max = arr[i];
        }
        var range = max - min || 1;
        var out = [];
        for (var j = 0; j < arr.length; j++) out.push((arr[j] - min) / range);
        return out;
    }

    var normCurrent = normalize(currentPrices);
    var normMatch = normalize(matchPrices);

    var matchW = futurePrices.length ? Math.round(w * 0.65) : w;
    var futureW = w - matchW;

    function polyline(norm, startX, segW, color, dashed, width) {
        if (!norm.length) return '';
        var pts = [];
        for (var i = 0; i < norm.length; i++) {
            var x = startX + (i / Math.max(norm.length - 1, 1)) * segW;
            var y = pad + (1 - norm[i]) * (h - 2 * pad);
            pts.push(x.toFixed(1) + ',' + y.toFixed(1));
        }
        return '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + color + '" stroke-width="' + (width || 2) + '"' +
            (dashed ? ' stroke-dasharray="6,4"' : '') + ' stroke-linecap="round" stroke-linejoin="round" />';
    }

    var svg = '<svg style="width:100%;height:40px" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">';
    if (futurePrices.length) {
        svg += '<line x1="' + matchW + '" y1="0" x2="' + matchW + '" y2="' + h + '" stroke="#30363d" stroke-width="1" stroke-dasharray="4,4" />';
    }
    svg += polyline(normCurrent, 0, matchW, '#58a6ff', false, 2.5);
    svg += polyline(normMatch, 0, matchW, 'rgba(201,209,217,0.6)', false, 2);
    if (futurePrices.length) {
        var normFuture = normalize(futurePrices);
        var futureColor = futurePrices[futurePrices.length - 1] >= futurePrices[0] ? '#26a69a' : '#ef5350';
        svg += polyline(normFuture, matchW, futureW, futureColor, true, 2);
    }
    svg += '</svg>';
    return svg;
}

var _patternHighlightId = null;

// ── Ghost candle canvas overlay ──────────────────────────────────
// Draws the current pattern's candles (orange) on a canvas layer
// on top of the chart. Redraws automatically on scroll / zoom.

var _ghostCanvas = null;
var _ghostCtx = null;
var _ghostMatchIdx = -1;
var _ghostSubscribed = false;

function _navigateToPatternMatch(matchIdx) {
    if (typeof klineChart === 'undefined' || !klineChart) return;
    if (!_patternMatchData) return;
    var matches = _patternMatchData.matches || [];
    if (matchIdx < 0 || matchIdx >= matches.length) return;
    var match = matches[matchIdx];

    var dataList = klineChart.getDataList();
    if (!dataList || !dataList.length) return;

    var targetTs = new Date(match.date).getTime();
    var endTs = new Date(match.end_date || match.date).getTime();

    // Check if target date is within chart data range (generous tolerance for HTF)
    var chartStart = dataList[0].timestamp;
    var chartEnd = dataList[dataList.length - 1].timestamp;
    var toleranceMs = 10 * 86400000; // 10 days tolerance
    if (targetTs < chartStart - toleranceMs || targetTs > chartEnd + toleranceMs) {
        console.warn('[Pattern] Target date outside chart range:', new Date(targetTs).toISOString(),
            'chart:', new Date(chartStart).toISOString(), '→', new Date(chartEnd).toISOString());
        return;
    }

    // Find closest data indices
    var startIdx = -1, endIdx = -1;
    var minDiff = Infinity;
    for (var i = 0; i < dataList.length; i++) {
        var diff = Math.abs(dataList[i].timestamp - targetTs);
        if (diff < minDiff) { minDiff = diff; startIdx = i; }
    }
    minDiff = Infinity;
    for (var j = 0; j < dataList.length; j++) {
        var diff2 = Math.abs(dataList[j].timestamp - endTs);
        if (diff2 < minDiff) { minDiff = diff2; endIdx = j; }
    }
    if (startIdx === -1) {
        console.warn('[Pattern] Could not find startIdx for', match.date);
        return;
    }
    if (endIdx === -1) endIdx = startIdx;

    // Clear previous
    _clearPatternHighlight();

    // Wait for browser to reflow after panel shrink, THEN resize + scroll + draw
    _ghostMatchIdx = matchIdx;
    requestAnimationFrame(function() {
        klineChart.resize();

        // Another frame for the chart to finish its internal resize
        requestAnimationFrame(function() {
            // scrollToDataIndex puts the given index at the RIGHT edge.
            // Place endIdx near the right with padding so full pattern is visible.
            klineChart.scrollToDataIndex(Math.min(endIdx + 8, dataList.length - 1));

            // Highlight rectangle
            var high = -Infinity, low = Infinity;
            for (var k = startIdx; k <= endIdx && k < dataList.length; k++) {
                if (dataList[k].high > high) high = dataList[k].high;
                if (dataList[k].low < low) low = dataList[k].low;
            }
            try {
                _patternHighlightId = klineChart.createOverlay({
                    name: 'rect',
                    groupId: 'pattern_highlight',
                    lock: true,
                    points: [
                        { timestamp: dataList[startIdx].timestamp, value: high },
                        { timestamp: dataList[endIdx].timestamp, value: low }
                    ],
                    styles: {
                        polygon: { color: 'rgba(255, 152, 0, 0.06)', borderColor: 'rgba(255, 152, 0, 0.25)', borderSize: 1 }
                    }
                });
            } catch (e) {}

            // Ghost candles — one more frame so scroll has taken effect
            requestAnimationFrame(function() {
                _ensureGhostCanvas();
                _drawGhostCandlesCanvas();

                if (!_ghostSubscribed) {
                    try {
                        klineChart.subscribeAction('onVisibleRangeChange', _drawGhostCandlesCanvas);
                        _ghostSubscribed = true;
                    } catch (e) {}
                }
            });
        });
    });
}

function _ensureGhostCanvas() {
    var container = document.getElementById('kline-chart-container');
    if (!container) return;
    if (_ghostCanvas && _ghostCanvas.parentElement === container) {
        // Resize to match container
        var dpr = window.devicePixelRatio || 1;
        _ghostCanvas.width = container.clientWidth * dpr;
        _ghostCanvas.height = container.clientHeight * dpr;
        _ghostCanvas.style.width = container.clientWidth + 'px';
        _ghostCanvas.style.height = container.clientHeight + 'px';
        _ghostCtx = _ghostCanvas.getContext('2d');
        _ghostCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
        return;
    }
    // Create canvas
    var dpr = window.devicePixelRatio || 1;
    var canvas = document.createElement('canvas');
    canvas.id = 'pattern-ghost-canvas';
    canvas.width = container.clientWidth * dpr;
    canvas.height = container.clientHeight * dpr;
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:' +
        container.clientWidth + 'px;height:' + container.clientHeight +
        'px;pointer-events:none;z-index:10;';
    container.appendChild(canvas);
    _ghostCanvas = canvas;
    _ghostCtx = canvas.getContext('2d');
    _ghostCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function _drawGhostCandlesCanvas() {
    if (!_ghostCanvas) return;
    if (typeof klineChart === 'undefined' || !klineChart) return;
    if (!_patternMatchData || _ghostMatchIdx < 0) return;

    var container = document.getElementById('kline-chart-container');
    if (!container) return;

    // Re-sync canvas size every frame (handles resize/reflow)
    var dpr = window.devicePixelRatio || 1;
    var cw = container.clientWidth;
    var ch = container.clientHeight;
    if (_ghostCanvas.clientWidth !== cw || _ghostCanvas.clientHeight !== ch) {
        _ghostCanvas.width = cw * dpr;
        _ghostCanvas.height = ch * dpr;
        _ghostCanvas.style.width = cw + 'px';
        _ghostCanvas.style.height = ch + 'px';
        _ghostCtx = _ghostCanvas.getContext('2d');
        _ghostCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    if (!_ghostCtx) return;

    var matches = _patternMatchData.matches || [];
    if (_ghostMatchIdx >= matches.length) return;
    var match = matches[_ghostMatchIdx];

    var ohlcByTf = _patternMatchData.current_ohlc_by_tf || {};
    var curOhlc = ohlcByTf[match.source_tf];
    if (!curOhlc || !curOhlc.length) return;

    var dataList = klineChart.getDataList();
    if (!dataList || !dataList.length) return;

    // Find start index — binary search (dataList is timestamp-sorted); this handler
    // re-runs on every visible-range change while the ghost preview is showing.
    var targetTs = new Date(match.date).getTime();
    var lo = 0, hi = dataList.length - 1;
    while (lo < hi) {
        var mid = (lo + hi) >> 1;
        if (dataList[mid].timestamp < targetTs) lo = mid + 1; else hi = mid;
    }
    var startIdx = lo;
    if (startIdx > 0 && Math.abs(dataList[startIdx - 1].timestamp - targetTs) <= Math.abs(dataList[startIdx].timestamp - targetTs)) {
        startIdx = startIdx - 1;
    }

    var endIdx = Math.min(startIdx + curOhlc.length - 1, dataList.length - 1);

    // Historical range
    var histHigh = -Infinity, histLow = Infinity;
    for (var k = startIdx; k <= endIdx && k < dataList.length; k++) {
        if (dataList[k].high > histHigh) histHigh = dataList[k].high;
        if (dataList[k].low < histLow) histLow = dataList[k].low;
    }
    // Current pattern range
    var curHigh = -Infinity, curLow = Infinity;
    for (var m = 0; m < curOhlc.length; m++) {
        if (curOhlc[m].h > curHigh) curHigh = curOhlc[m].h;
        if (curOhlc[m].l < curLow) curLow = curOhlc[m].l;
    }
    var curRange = curHigh - curLow || 1;
    var histRange = histHigh - histLow || 1;

    // Scale function: map current OHLC value into historical price space
    function scaleVal(v) {
        return histLow + ((v - curLow) / curRange) * histRange;
    }

    // Get bar space for candle width
    var barSpace = klineChart.getBarSpace();
    var halfBar = barSpace.bar * 0.35;

    // Clear canvas
    _ghostCtx.clearRect(0, 0, cw, ch);

    // Draw each ghost candle
    var bullBody = 'rgba(255, 152, 0, 0.55)';
    var bullBorder = 'rgba(255, 152, 0, 0.85)';
    var bearBody = 'rgba(255, 109, 0, 0.55)';
    var bearBorder = 'rgba(255, 109, 0, 0.85)';
    var wickColor = 'rgba(255, 152, 0, 0.6)';

    var ctx = _ghostCtx;
    for (var c = 0; c < curOhlc.length; c++) {
        var chartIdx = startIdx + c;
        if (chartIdx >= dataList.length) break;

        var bar = curOhlc[c];
        var sO = scaleVal(bar.o);
        var sH = scaleVal(bar.h);
        var sL = scaleVal(bar.l);
        var sC = scaleVal(bar.c);

        // Convert to pixel coordinates (relative to chart's internal canvas)
        var pxO = klineChart.convertToPixel({ timestamp: dataList[chartIdx].timestamp, value: sO }, { paneId: 'candle_pane' });
        var pxH = klineChart.convertToPixel({ timestamp: dataList[chartIdx].timestamp, value: sH }, { paneId: 'candle_pane' });
        var pxL = klineChart.convertToPixel({ timestamp: dataList[chartIdx].timestamp, value: sL }, { paneId: 'candle_pane' });
        var pxC = klineChart.convertToPixel({ timestamp: dataList[chartIdx].timestamp, value: sC }, { paneId: 'candle_pane' });

        if (!pxO || !pxH || !pxL || !pxC) continue;
        if (isNaN(pxO.x) || isNaN(pxO.y)) continue;

        var x = pxO.x;
        var yO = pxO.y;
        var yH = pxH.y;
        var yL = pxL.y;
        var yC = pxC.y;
        var bull = bar.c >= bar.o;

        // Wick
        ctx.beginPath();
        ctx.strokeStyle = wickColor;
        ctx.lineWidth = 1.5;
        ctx.moveTo(x, yH);
        ctx.lineTo(x, yL);
        ctx.stroke();

        // Body
        var bodyTop = Math.min(yO, yC);
        var bodyH = Math.max(Math.abs(yO - yC), 1.5);
        ctx.fillStyle = bull ? bullBody : bearBody;
        ctx.fillRect(x - halfBar, bodyTop, halfBar * 2, bodyH);
        ctx.strokeStyle = bull ? bullBorder : bearBorder;
        ctx.lineWidth = 1;
        ctx.strokeRect(x - halfBar, bodyTop, halfBar * 2, bodyH);
    }
}

function _clearPatternHighlight() {
    // Remove KLineChart overlay (highlight rect)
    if (typeof klineChart !== 'undefined' && klineChart) {
        if (_patternHighlightId) {
            try { klineChart.removeOverlay({ id: _patternHighlightId }); } catch (e) {}
            _patternHighlightId = null;
        }
        try { klineChart.removeOverlay({ groupId: 'pattern_highlight' }); } catch (e) {}
    }
    // Remove ghost canvas
    _ghostMatchIdx = -1;
    if (_ghostCanvas) {
        try { _ghostCanvas.parentElement.removeChild(_ghostCanvas); } catch (e) {}
        _ghostCanvas = null;
        _ghostCtx = null;
    }
    if (_ghostSubscribed && typeof klineChart !== 'undefined' && klineChart) {
        try { klineChart.unsubscribeAction('onVisibleRangeChange', _drawGhostCandlesCanvas); } catch (e) {}
        _ghostSubscribed = false;
    }
}

function _shrinkPanelForPattern() {
    var panel = document.getElementById('right-panel');
    if (!panel) return;
    var currentWidth = parseInt(panel.style.width, 10);
    if (currentWidth > 420) {
        _patternPanelShrunk = true;
        panel.style.width = '400px';
        try { localStorage.setItem('panelWidth', '400'); } catch(e) {}
    }
}

function _restorePanelFromPattern() {
    if (!_patternPanelShrunk) return;
    _patternPanelShrunk = false;
    var expandedWidth = Math.max(Math.round(window.innerWidth * 0.55), 800);
    var panel = document.getElementById('right-panel');
    if (panel) {
        panel.style.width = expandedWidth + 'px';
        try { localStorage.setItem('panelWidth', String(expandedWidth)); } catch(e) {}
    }
}

// Legacy alias for backward compat (auto-capture still calls _patternsFindSimilar externally)
function _patternsFindSimilar() { _patternsInChat(); }

// ============================================================
// Auto-Capture Integration (called from kline-drawings.js)
// ============================================================

/**
 * Auto-open a journal entry when a position drawing is placed.
 * Returns a promise resolving to the journal entry ID.
 */
function autoOpenJournalEntry(drawingData) {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    return fetch('/api/ai/journal/open', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify(drawingData)
    }).then(function(r) { return r.json(); })
    .then(function(d) {
        // Refresh journal and switch to positions tab
        if (_journalLoaded) {
            _switchJournalTab('positions');
            refreshJournal();
        }
        return d.id || null;
    }).catch(function() { return null; });
}

/**
 * Update journal entry when position drawing is edited (SL/TP moved).
 */
function updateJournalFromDrawing(journalId, updates) {
    if (!journalId) return;
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    fetch('/api/ai/journal/' + journalId, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify(updates)
    }).then(function() {
        if (_journalLoaded) refreshJournal();
    }).catch(function(e) {
        if (typeof window.carracLog === 'function') window.carracLog('journal', 'journal SL/TP update from drawing failed — journal now stale vs chart', String((e && e.message) || e));
        if (typeof showToast === 'function') showToast('Journal SL/TP update could not be saved — check your connection and try again');
    });
}

/**
 * Create TP and SL alerts linked to a journal entry.
 * Returns a promise resolving to {tp_alert_id, sl_alert_id}.
 */
function createJournalAlerts(journalId, symbol, direction, targetPrice, stopPrice) {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    var promises = [];

    if (targetPrice) {
        var tpCondition = direction === 'long' ? 'above' : 'below';
        promises.push(
            fetch('/api/alerts', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                credentials: 'same-origin',
                body: JSON.stringify({
                    symbol: symbol,
                    condition: tpCondition,
                    price: targetPrice,
                    note: '[auto] TP for journal #' + journalId,
                    frequency: 'once',
                    journal_id: journalId,
                    auto_close: 'tp_hit'
                })
            }).then(function(r) { return r.json(); })
        );
    }

    if (stopPrice) {
        var slCondition = direction === 'long' ? 'below' : 'above';
        promises.push(
            fetch('/api/alerts', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                credentials: 'same-origin',
                body: JSON.stringify({
                    symbol: symbol,
                    condition: slCondition,
                    price: stopPrice,
                    note: '[auto] SL for journal #' + journalId,
                    frequency: 'once',
                    journal_id: journalId,
                    auto_close: 'sl_hit'
                })
            }).then(function(r) { return r.json(); })
        );
    }

    return Promise.all(promises);
}

/**
 * Delete alerts linked to a journal entry (for re-creation on edit).
 */
function deleteJournalAlerts(journalId) {
    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    return fetch('/api/alerts?active=true', {headers: {'X-CSRFToken': csrf}})
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var alerts = data.alerts || [];
        var deletePromises = [];
        alerts.forEach(function(a) {
            if (a.journal_id === journalId) {
                deletePromises.push(
                    fetch('/api/alerts/' + a.id, {
                        method: 'DELETE',
                        headers: {'X-CSRFToken': csrf},
                    })
                );
            }
        });
        return Promise.all(deletePromises);
    });
}

// ============================================================
// Markdown Renderer
// ============================================================

/**
 * Lightweight markdown to HTML renderer for AI responses.
 * Handles: **bold**, *italic*, `code`, ```blocks```, ## headers, - lists, 1. lists.
 * XSS-safe: HTML entities escaped before any processing.
 */
function _aiRenderMarkdown(text) {
    if (!text) return '';

    var html = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

    html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, function(_, lang, code) {
        return '<pre class="ai-code"><code>' + code.trim() + '</code></pre>';
    });

    var lines = html.split('\n');
    var output = [];
    var inList = false;
    var listType = '';
    var inTable = false;
    var tableHeaderDone = false;

    for (var i = 0; i < lines.length; i++) {
        var line = lines[i];

        // Table rows: lines starting with |
        if (/^\|(.+)\|/.test(line.trim())) {
            _closeList();
            // Separator row (|---|---|---| or |:---|:---:|) — skip but mark header done
            if (/^\|[\s:]*-{2,}[\s:]*\|/.test(line.trim())) {
                tableHeaderDone = true;
                continue;
            }
            if (!inTable) {
                output.push('<table class="ai-table">');
                inTable = true;
                tableHeaderDone = false;
            }
            var cells = line.trim().replace(/^\||\|$/g, '').split('|');
            var tag = (!tableHeaderDone) ? 'th' : 'td';
            // Detect verdict row by keywords
            var rowText = line.toUpperCase();
            var isVerdict = /VERDICT|TOTAL/.test(rowText) && /STRONG|GOOD|NEUTRAL|WEAK|AVOID|\d+\s*\/\s*12/.test(rowText);
            var verdictClass = '';
            if (isVerdict) {
                if (/STRONG/.test(rowText)) verdictClass = 'ai-verdict-strong';
                else if (/GOOD/.test(rowText)) verdictClass = 'ai-verdict-good';
                else if (/NEUTRAL/.test(rowText)) verdictClass = 'ai-verdict-neutral';
                else if (/WEAK/.test(rowText)) verdictClass = 'ai-verdict-weak';
                else if (/AVOID/.test(rowText)) verdictClass = 'ai-verdict-avoid';
            }
            output.push('<tr' + (verdictClass ? ' class="' + verdictClass + '"' : '') + '>');
            for (var c = 0; c < cells.length; c++) {
                output.push('<' + tag + '>' + _inlineFormat(cells[c].trim()) + '</' + tag + '>');
            }
            output.push('</tr>');
            continue;
        } else if (inTable) {
            output.push('</table>');
            inTable = false;
            tableHeaderDone = false;
        }

        if (/^####\s+(.+)/.test(line)) {
            _closeList();
            output.push('<h6 class="ai-h">' + _inlineFormat(RegExp.$1) + '</h6>');
            continue;
        }
        if (/^###\s+(.+)/.test(line)) {
            _closeList();
            output.push('<h5 class="ai-h">' + _inlineFormat(RegExp.$1) + '</h5>');
            continue;
        }
        if (/^##\s+(.+)/.test(line)) {
            _closeList();
            output.push('<h4 class="ai-h">' + _inlineFormat(RegExp.$1) + '</h4>');
            continue;
        }
        if (/^#\s+(.+)/.test(line)) {
            _closeList();
            output.push('<h4 class="ai-h">' + _inlineFormat(RegExp.$1) + '</h4>');
            continue;
        }

        if (/^---+$/.test(line.trim())) {
            _closeList();
            output.push('<hr class="ai-hr">');
            continue;
        }

        if (/^[\s]*[-*]\s+(.+)/.test(line)) {
            if (!inList || listType !== 'ul') {
                _closeList();
                output.push('<ul class="ai-list">');
                inList = true;
                listType = 'ul';
            }
            output.push('<li>' + _inlineFormat(RegExp.$1) + '</li>');
            continue;
        }

        if (/^[\s]*\d+[.)]\s+(.+)/.test(line)) {
            if (!inList || listType !== 'ol') {
                _closeList();
                output.push('<ol class="ai-list">');
                inList = true;
                listType = 'ol';
            }
            output.push('<li>' + _inlineFormat(RegExp.$1) + '</li>');
            continue;
        }

        if (line.trim() === '') {
            // Don't close list if next non-empty line continues same list type
            if (inList) {
                var peek = '';
                for (var j = i + 1; j < lines.length; j++) {
                    if (lines[j].trim() !== '') { peek = lines[j]; break; }
                }
                var nextIsOl = /^[\s]*\d+[.)]\s+/.test(peek);
                var nextIsUl = /^[\s]*[-*]\s+/.test(peek);
                if ((listType === 'ol' && nextIsOl) || (listType === 'ul' && nextIsUl)) {
                    continue; // skip blank line, keep list open
                }
            }
            _closeList();
            output.push('<br>');
            continue;
        }

        _closeList();
        output.push('<span>' + _inlineFormat(line) + '</span><br>');
    }
    _closeList();
    if (inTable) output.push('</table>');

    function _closeList() {
        if (inList) {
            output.push(listType === 'ol' ? '</ol>' : '</ul>');
            inList = false;
            listType = '';
        }
    }

    var result = output.join('');
    if (typeof DOMPurify !== 'undefined') {
        result = DOMPurify.sanitize(result, {
            ADD_TAGS: ['details', 'summary'],
            ADD_ATTR: ['class']
        });
    } else {
        // DOMPurify unavailable — the HTML was built from an escaped base, but
        // _inlineFormat reintroduces a small set of tags (<strong>,<em>,<s>,<code>).
        // Without a sanitizer we cannot safely allow any markup, so strip all tags
        // so that LLM output cannot execute as HTML.
        result = result.replace(/<[^>]*>/g, '');
    }
    return result;
}

/** Inline markdown: **bold**, *italic*, `code`, ~~strike~~ */
function _inlineFormat(text) {
    text = text.replace(/`([^`]+)`/g, '<code class="ai-inline-code">$1</code>');
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    text = text.replace(/~~([^~]+)~~/g, '<s>$1</s>');
    return text;
}

// ============================================================
// Journal Analytics
// ============================================================

var _analyticsData = null;
var _analyticsLoaded = false;

function _toggleAnalytics() {
    var panel = document.getElementById('journal-analytics-panel');
    if (!panel) return;
    var visible = panel.style.display !== 'none';
    panel.style.display = visible ? 'none' : 'block';
    if (!visible && !_analyticsLoaded) {
        var days = parseInt(document.getElementById('journal-analytics-days').value) || 30;
        _loadAnalytics(days);
    }
}

function _loadAnalytics(days) {
    var content = document.getElementById('journal-analytics-content');
    if (!content) return;
    content.innerHTML = '<div class="journal-empty">Loading...</div>';
    _analyticsLoaded = true;

    var csrf = '';
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) csrf = meta.content;

    fetch('/api/ai/journal/analytics?days=' + days, {headers: {'X-CSRFToken': csrf}})
    .then(function(r) { return r.json(); })
    .then(function(data) {
        _analyticsData = data;
        _renderAnalytics(data);
        _renderKellySection(data);
    }).catch(function() {
        content.innerHTML = '<div class="journal-empty">Failed to load analytics</div>';
    });
}

function _renderAnalytics(data) {
    var el = document.getElementById('journal-analytics-content');
    if (!el) return;

    if (!data.trades) {
        el.innerHTML = '<div class="journal-empty">Close some trades to see analytics</div>';
        return;
    }

    var html = '';

    // Key metrics row
    var exp = data.expectancy != null ? (data.expectancy >= 0 ? '+' : '') + data.expectancy + 'R' : 'N/A';
    var pf = data.profit_factor != null ? data.profit_factor.toFixed(1) : 'INF';
    var hold = data.avg_hold_hours != null ? (data.avg_hold_hours < 24 ? data.avg_hold_hours.toFixed(0) + 'h' : (data.avg_hold_hours / 24).toFixed(1) + 'd') : 'N/A';
    html += '<div class="analytics-metrics-row">';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Exp</span><span class="analytics-metric-value" style="color:' + (data.expectancy >= 0 ? '#26a69a' : '#ef5350') + '">' + exp + '</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">PF</span><span class="analytics-metric-value">' + pf + '</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Hold</span><span class="analytics-metric-value">' + hold + '</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Avg W</span><span class="analytics-metric-value" style="color:#26a69a">+' + (data.avg_win_rr || 0) + 'R</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Avg L</span><span class="analytics-metric-value" style="color:#ef5350">-' + (data.avg_loss_rr || 0) + 'R</span></div>';
    html += '</div>';

    // Streaks
    var cs = data.current_streak || {};
    var csText = cs.type === 'win' ? cs.count + 'W' : cs.type === 'loss' ? cs.count + 'L' : '-';
    html += '<div class="analytics-metrics-row">';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Current</span><span class="analytics-metric-value">' + csText + '</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Best W</span><span class="analytics-metric-value" style="color:#26a69a">' + (data.longest_win_streak || 0) + '</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Worst L</span><span class="analytics-metric-value" style="color:#ef5350">' + (data.longest_loss_streak || 0) + '</span></div>';
    html += '</div>';

    // Breakdowns
    html += _renderBreakdown('By Setup', data.by_setup || {});
    html += _renderBreakdown('By Asset', data.by_asset || {});
    html += _renderBreakdown('By Direction', data.by_direction || {});
    html += _renderBreakdown('By Timeframe', data.by_timeframe || {});
    html += _renderBreakdown('By Weekday', data.by_weekday || {});

    // Equity curve
    var monthly = data.monthly || [];
    if (monthly.length > 0) {
        html += '<details class="analytics-breakdown"><summary class="analytics-breakdown-title">Equity Curve</summary>';
        html += '<div class="equity-bar-chart">';
        var maxAbs = 0;
        monthly.forEach(function(m) { maxAbs = Math.max(maxAbs, Math.abs(m.net_rr)); });
        if (maxAbs === 0) maxAbs = 1;
        monthly.forEach(function(m) {
            var pct = Math.abs(m.net_rr) / maxAbs * 100;
            var color = m.net_rr >= 0 ? '#26a69a' : '#ef5350';
            html += '<div class="equity-bar-row">';
            html += '<span class="equity-bar-label">' + m.month + '</span>';
            html += '<div class="equity-bar-track"><div class="equity-bar" style="width:' + pct + '%;background:' + color + '"></div></div>';
            html += '<span class="equity-bar-value" style="color:' + color + '">' + (m.net_rr >= 0 ? '+' : '') + m.net_rr + 'R</span>';
            html += '</div>';
        });
        html += '</div></details>';
    }

    el.innerHTML = html;
}

function _renderBreakdown(title, groups) {
    var keys = Object.keys(groups);
    if (!keys.length) return '';
    var html = '<details class="analytics-breakdown"><summary class="analytics-breakdown-title">' + title + '</summary>';
    keys.forEach(function(k) {
        var g = groups[k];
        var wrColor = (g.win_rate || 0) >= 50 ? '#26a69a' : '#ef5350';
        var rrColor = (g.avg_rr || 0) >= 0 ? '#26a69a' : '#ef5350';
        html += '<div class="analytics-breakdown-row">';
        html += '<span class="abd-name">' + k + '</span>';
        html += '<span class="abd-count">' + g.trades + '</span>';
        html += '<span class="abd-wr" style="color:' + wrColor + '">' + (g.win_rate || 0) + '%</span>';
        html += '<span class="abd-rr" style="color:' + rrColor + '">' + (g.avg_rr >= 0 ? '+' : '') + (g.avg_rr || 0) + 'R</span>';
        html += '</div>';
    });
    html += '</details>';
    return html;
}

// ============================================================
// Kelly Criterion Position Sizer
// ============================================================

function _computeKelly(analytics, fractionPct) {
    if (!analytics || analytics.trades < 5) return null;
    var wr = (analytics.win_rate || 0) / 100;
    var lr = 1 - wr;
    var avgWin = analytics.avg_win_rr || 0;
    var avgLoss = analytics.avg_loss_rr || 0;
    if (avgWin <= 0) return null;

    var kellyPct = ((wr * avgWin) - (lr * avgLoss)) / avgWin;
    var fractionalPct = kellyPct * (fractionPct / 100);
    return {kellyPct: kellyPct, fractionalPct: fractionalPct};
}

function _renderKellySection(analytics) {
    var section = document.getElementById('journal-kelly-section');
    if (!section) return;

    if (!analytics || analytics.trades < 5) {
        section.style.display = 'none';
        var resultEl = document.getElementById('kelly-result');
        if (resultEl) resultEl.innerHTML = '<div class="kelly-na">Need 5+ closed trades</div>';
        return;
    }

    section.style.display = 'block';
    _updateKellyResult();
}

function _updateKellyResult() {
    var resultEl = document.getElementById('kelly-result');
    if (!resultEl || !_analyticsData) return;

    var fractionPct = parseInt(document.getElementById('kelly-fraction').value) || 25;
    var kelly = _computeKelly(_analyticsData, fractionPct);

    if (!kelly) {
        resultEl.innerHTML = '<div class="kelly-na">Need 5+ trades with positive expectancy</div>';
        document.getElementById('kelly-trade-calc').style.display = 'none';
        return;
    }

    if (kelly.kellyPct <= 0) {
        resultEl.innerHTML = '<div class="kelly-warning">System not profitable (Kelly &le; 0). Do not risk capital.</div>';
        document.getElementById('kelly-trade-calc').style.display = 'none';
        return;
    }

    var html = '<div class="analytics-metrics-row">';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Full Kelly</span><span class="analytics-metric-value">' + (kelly.kellyPct * 100).toFixed(1) + '%</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Fractional</span><span class="analytics-metric-value" style="color:#3fb950">' + (kelly.fractionalPct * 100).toFixed(1) + '%</span></div>';

    var accountSize = parseFloat(document.getElementById('kelly-account-size').value) || 0;
    if (accountSize > 0) {
        var riskAmt = accountSize * kelly.fractionalPct;
        html += '<div class="analytics-metric"><span class="analytics-metric-label">Risk $</span><span class="analytics-metric-value" style="color:#d29922">$' + riskAmt.toFixed(0) + '</span></div>';
    }
    html += '</div>';

    if (kelly.kellyPct > 1) {
        html += '<div class="kelly-warning">Kelly &gt;100% — extreme leverage implied. Use smaller fraction.</div>';
    }

    resultEl.innerHTML = html;

    // Show trade calculator if we have a risk amount
    var tradeCalc = document.getElementById('kelly-trade-calc');
    if (tradeCalc) tradeCalc.style.display = accountSize > 0 ? 'block' : 'none';

    _updateKellyTrade();
}

function _updateKellyTrade() {
    var resultEl = document.getElementById('kelly-trade-result');
    if (!resultEl || !_analyticsData) return;

    var entry = parseFloat(document.getElementById('kelly-entry').value);
    var stop = parseFloat(document.getElementById('kelly-stop').value);
    if (!entry || !stop || entry === stop) { resultEl.innerHTML = ''; return; }

    var fractionPct = parseInt(document.getElementById('kelly-fraction').value) || 25;
    var kelly = _computeKelly(_analyticsData, fractionPct);
    if (!kelly || kelly.kellyPct <= 0) { resultEl.innerHTML = ''; return; }

    var accountSize = parseFloat(document.getElementById('kelly-account-size').value) || 0;
    if (!accountSize) { resultEl.innerHTML = ''; return; }

    var riskPct = Math.abs(entry - stop) / entry;
    var riskAmt = accountSize * kelly.fractionalPct;
    var positionSize = riskAmt / riskPct;
    var leverage = positionSize / accountSize;

    var html = '<div class="analytics-metrics-row" style="margin-top:4px">';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Position</span><span class="analytics-metric-value">$' + positionSize.toFixed(0) + '</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Leverage</span><span class="analytics-metric-value">' + leverage.toFixed(2) + 'x</span></div>';
    html += '<div class="analytics-metric"><span class="analytics-metric-label">Risk %</span><span class="analytics-metric-value">' + (riskPct * 100).toFixed(2) + '%</span></div>';
    html += '</div>';
    resultEl.innerHTML = html;
}

// ============================================================
// Trading Profile (redirects to settings page)
// ============================================================

/** Get Kelly suggestion for position drawing integration */
function _getKellySuggestion(entryPrice, stopPrice) {
    if (!_analyticsData || !entryPrice || !stopPrice || entryPrice === stopPrice) return null;
    var fractionPct = parseInt((document.getElementById('kelly-fraction') || {}).value) || 25;
    var kelly = _computeKelly(_analyticsData, fractionPct);
    if (!kelly || kelly.kellyPct <= 0) return null;
    var accountSize = parseFloat(localStorage.getItem('carrac_account_size')) || 0;
    if (!accountSize) return null;

    var riskPct = Math.abs(entryPrice - stopPrice) / entryPrice;
    var riskAmt = accountSize * kelly.fractionalPct;
    var positionSize = riskAmt / riskPct;
    return {
        riskAmount: riskAmt,
        positionSize: positionSize,
        fractionalPct: kelly.fractionalPct,
    };
}


// ================================================================
// Position Watch
// ================================================================

var _positionWatchEnabled = false;
var _positionWatchTimer = null;

function initPositionWatch() {
    var toggle = document.getElementById('jp-watch-enabled');
    if (!toggle) return;

    // Load preference
    _positionWatchEnabled = localStorage.getItem('positionWatchEnabled') === 'true';
    toggle.checked = _positionWatchEnabled;

    var content = document.getElementById('jp-watch-content');

    toggle.addEventListener('change', function() {
        _positionWatchEnabled = this.checked;
        localStorage.setItem('positionWatchEnabled', String(_positionWatchEnabled));
        if (_positionWatchEnabled) {
            _refreshPositionWatch();
            _startWatchPolling();
        } else {
            _stopWatchPolling();
            if (content) content.innerHTML = '<div class="jp-watch-disabled">Toggle on to monitor open positions</div>';
        }
    });

    if (_positionWatchEnabled) {
        _refreshPositionWatch();
        _startWatchPolling();
    } else if (content) {
        content.innerHTML = '<div class="jp-watch-disabled">Toggle on to monitor open positions</div>';
    }
}

function _startWatchPolling() {
    _stopWatchPolling();
    _positionWatchTimer = setInterval(function() {
        if (_positionWatchEnabled) _refreshPositionWatch();
    }, 60000); // 60s
}

function _stopWatchPolling() {
    if (_positionWatchTimer) { clearInterval(_positionWatchTimer); _positionWatchTimer = null; }
}

function _refreshPositionWatch() {
    var content = document.getElementById('jp-watch-content');
    if (!content) return;

    fetch('/api/journal/trades?include_open=true&days=1&limit=1', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data || !data.open || data.open.length === 0) {
                content.innerHTML = '<div class="jp-watch-empty">No open positions to monitor</div>';
                return;
            }
            _renderWatchCards(content, data.open);
        })
        .catch(function() {
            content.innerHTML = '<div class="jp-watch-empty">Could not load positions</div>';
        });
}

function _renderWatchCards(container, trades) {
    // Group trades by asset+direction (avoid showing 3 QQQ LONG cards)
    var groups = {};
    trades.forEach(function(t) {
        var key = (t.asset || '') + ':' + (t.direction || 'long');
        if (!groups[key]) {
            groups[key] = { asset: t.asset, direction: t.direction || 'long', count: 0 };
        }
        groups[key].count++;
    });

    var positions = Object.values(groups);
    if (positions.length === 0) {
        container.innerHTML = '<div class="jp-watch-empty">No open positions to monitor</div>';
        return;
    }

    // Fetch current indicator state from assets cache (faster than matrix API)
    var stateBySymbol = {};
    for (var sym in allAssets) {
        stateBySymbol[sym] = allAssets[sym];
    }

    var html = '';
    positions.forEach(function(pos) {
        var sym = pos.asset;
        var dir = pos.direction;
        var asset = stateBySymbol[sym];

        html += '<div class="jp-watch-card">';
        html += '<div class="jp-watch-card-header">';
        html += '<span class="jp-watch-symbol" data-symbol="' + escapeHtml(sym) + '">' + escapeHtml(sym) + '</span>';
        html += '<span class="jp-watch-dir jp-watch-dir-' + dir + '">' + dir.toUpperCase() + '</span>';
        if (pos.count > 1) html += '<span class="jp-watch-count">×' + pos.count + '</span>';
        html += '</div>';

        if (!asset) {
            html += '<div class="jp-watch-no-data">No indicator data</div></div>';
            return;
        }

        // Read indicators from cached asset data
        var emaT = (asset.ema_ribbon || {}).trend || 'neutral';
        // Map 'bull'/'bear' to 'bullish'/'bearish' (asset data uses short form)
        if (emaT === 'bull') emaT = 'bullish';
        if (emaT === 'bear') emaT = 'bearish';

        var macdH = (asset.macd || {}).histogram || 0;
        var macdDir = macdH > 0 ? 'bullish' : macdH < 0 ? 'bearish' : 'flat';

        var structT = (asset.structure || {}).structure_trend || 'unknown';
        // Map 'bullish'/'bearish' to 'uptrend'/'downtrend'
        if (structT === 'bullish') structT = 'uptrend';
        if (structT === 'bearish') structT = 'downtrend';

        var consS = 0;
        if (asset.consensio && asset.consensio.total) consS = asset.consensio.total.signal || 0;

        var rsiVal = asset.weekly_rsi;
        var rsiZone = rsiVal == null ? null :
            rsiVal < 30 ? 'oversold' : rsiVal > 70 ? 'overbought' : 'neutral';

        var indicators = [
            { label: 'EMA Ribbon', val: emaT, bullish: 'bullish', bearish: 'bearish' },
            { label: 'MACD', val: macdDir, bullish: 'bullish', bearish: 'bearish' },
            { label: 'Structure', val: structT, bullish: 'uptrend', bearish: 'downtrend' },
            { label: 'Consensio', val: consS, bullish: 1, bearish: -1, numeric: true },
            { label: 'RSI', val: rsiZone, bullish: dir === 'long' ? 'oversold' : 'overbought',
              bearish: dir === 'long' ? 'overbought' : 'oversold' },
        ];

        indicators.forEach(function(ind) {
            var status = 'neutral';
            var v = ind.val;
            if (ind.numeric) {
                if (dir === 'long') status = v > 0 ? 'good' : v < 0 ? 'bad' : 'neutral';
                else status = v < 0 ? 'good' : v > 0 ? 'bad' : 'neutral';
            } else {
                if (dir === 'long') status = v === ind.bullish ? 'good' : v === ind.bearish ? 'bad' : 'neutral';
                else status = v === ind.bearish ? 'good' : v === ind.bullish ? 'bad' : 'neutral';
            }

            var dot = status === 'good' ? '🟢' : status === 'bad' ? '🔴' : '🟡';
            var displayVal = v != null ? String(v) : '—';
            if (ind.numeric && v != null) displayVal = (v > 0 ? '+' : '') + v;

            html += '<div class="jp-watch-ind jp-watch-' + status + '">';
            html += '<span class="jp-watch-dot">' + dot + '</span>';
            html += '<span class="jp-watch-ind-label">' + ind.label + '</span>';
            html += '<span class="jp-watch-ind-val">' + displayVal + '</span>';
            html += '</div>';
        });

        html += '</div>';
    });

    container.innerHTML = html;

    // Watchlist symbol clicks — one delegated listener on the persistent container
    // (re-bound guard: this fires every ~60s poll, but the container div is reused).
    if (!container._watchSymbolClickBound) {
        container._watchSymbolClickBound = true;
        container.addEventListener('click', function(ev) {
            var el = ev.target.closest('.jp-watch-symbol');
            if (!el) return;
            var sym = el.getAttribute('data-symbol');
            if (sym) selectAssetFromSearch(sym);
        });
    }
}

// initPositionWatch is called from setViewMode('journal') in split-layout.js
