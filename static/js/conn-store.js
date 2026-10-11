/* conn-store.js — aggregate connection-state registry.
 * Loaded BEFORE the provider WS files (defer order). Providers report lifecycle
 * transitions; the dot + tooltip render the aggregate. PURE OBSERVER — zero
 * influence on any provider's reconnect behaviour. */
(function () {
    // Weight = symbols affected when the provider is unhealthy (tooltip ordering).
    // kraken (XMR) + gate (AKT/DEEP/ARC/CHEX/DSYNC/KIP/MLC/PIPPIN/PRIME) replaced
    // coincap — CoinCap retired its keyless public WS (permanent reconnect loop
    // = permanent amber dot). bybit (CC) is handled in table-tickers-ws.js.
    var PROVIDER_WEIGHT = { binance: 5, finnhub: 4, hyperliquid: 3, kraken: 2, gate: 2, ccxt: 1, bybit: 1 };

    var _state = {};
    Object.keys(PROVIDER_WEIGHT).forEach(function (p) {
        _state[p] = { state: 'init', lastMessageAt: 0, since: Date.now() };
    });

    // Pure: states = {provider: stateString}, onLine = navigator.onLine.
    // 'init' is neutral (provider never started — e.g. ccxt only runs for its
    // charts) EXCEPT when every provider is still init (page just loaded).
    function deriveAggregate(states, onLine) {
        if (!onLine) return 'offline';
        var keys = Object.keys(states);
        var allInit = keys.length > 0 && keys.every(function (k) { return states[k] === 'init'; });
        if (allInit) return 'init';
        if (states.binance === 'down') return 'offline';
        if (states.binance === 'reconnecting') return 'reconnecting';
        var degraded = keys.some(function (k) {
            return k !== 'binance' && (states[k] === 'down' || states[k] === 'reconnecting');
        });
        return degraded ? 'degraded' : 'live';
    }

    function _snapshotStates() {
        var s = {};
        Object.keys(_state).forEach(function (p) { s[p] = _state[p].state; });
        return s;
    }

    function _fmtTime(ts) {
        var d = new Date(ts);
        return d.getHours() + ':' + ('0' + d.getMinutes()).slice(-2);
    }

    function _renderDot() {
        try {
            var el = document.getElementById('conn-status-dot');
            if (!el) return;
            var agg = deriveAggregate(_snapshotStates(), navigator.onLine !== false);
            var COLOR = { live: '#26a69a', degraded: '#d29922', reconnecting: '#d29922', offline: '#ef5350', init: '#8b949e' };
            var PULSE = { reconnecting: true, offline: true };
            el.style.background = COLOR[agg] || '#8b949e';
            el.style.boxShadow = PULSE[agg] ? '0 0 0 3px ' + (COLOR[agg] + '44') : 'none';
            var lines = Object.keys(PROVIDER_WEIGHT)
                .sort(function (a, b) { return PROVIDER_WEIGHT[b] - PROVIDER_WEIGHT[a]; })
                .map(function (p) {
                    var s = _state[p];
                    var suffix = (s.state !== 'connected' && s.state !== 'init') ? ' since ' + _fmtTime(s.since) : '';
                    // 'init' = provider never needed yet (e.g. ccxt only runs
                    // for its charts) — show as 'idle' so it doesn't read stuck.
                    var label = s.state === 'init' ? 'idle' : s.state;
                    return p + ': ' + label + suffix;
                });
            el.title = 'Live data: ' + (agg === 'init' ? 'starting' : agg) + '\n' + lines.join('\n');
        } catch (e) {}
    }

    function report(provider, newState) {
        var prev = _state[provider];
        if (!prev) { _state[provider] = { state: newState, lastMessageAt: 0, since: Date.now() }; _renderDot(); return; }
        if (prev.state !== newState) {
            prev.state = newState;
            prev.since = Date.now();
            _renderDot();
        }
    }

    function heartbeat(provider) {
        if (_state[provider]) _state[provider].lastMessageAt = Date.now();
    }

    if (typeof window !== 'undefined') {
        window.CarracConn = { report: report, heartbeat: heartbeat, deriveAggregate: deriveAggregate, _state: _state };
        window.addEventListener('online', _renderDot);
        window.addEventListener('offline', _renderDot);
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { deriveAggregate: deriveAggregate };
    }
})();
