/**
 * Watchlist — server-primary with localStorage fallback.
 * Global: watchlistSymbols (array of symbol strings)
 */

var _watchlistLoaded = false;

// Groups are the source of truth; watchlistSymbols is DERIVED
// (ordered concat, deduped) so all flat consumers keep working untouched.
var watchlistGroups = [];

function deriveWatchlistFlat() {
    var flat = [];
    watchlistGroups.forEach(function(g) {
        (g.symbols || []).forEach(function(s) {
            if (flat.indexOf(s) === -1) flat.push(s);
        });
    });
    // mutate in place so every closure holding the array reference sees it
    watchlistSymbols.length = 0;
    Array.prototype.push.apply(watchlistSymbols, flat);
}

function _groupsFromFlat(symbols) {
    return [{ id: 'default', name: 'Watchlist', symbols: symbols.slice() }];
}

var _groupsSaveTimer = null;
var _groupsSaveInFlight = false;
function saveWatchlistGroups() {
    deriveWatchlistFlat();
    _saveLocal();
    try { localStorage.setItem('watchlistGroups', JSON.stringify(watchlistGroups)); } catch (e) {}
    if (_groupsSaveTimer) clearTimeout(_groupsSaveTimer);
    _groupsSaveTimer = setTimeout(function() {
        _groupsSaveTimer = null;
        _groupsSaveInFlight = true;
        fetch('/api/watchlist/groups', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({ groups: watchlistGroups })
        }).then(function(r) {
            _groupsSaveInFlight = false;
            // Surface rejected saves (validation 400s) — a silent failure here
            // means group edits revert on next reload with no signal why.
            if (r && !r.ok) {
                if (typeof window.carracLog === 'function') {
                    window.carracLog('watchlist', 'groups save rejected', { status: r.status });
                }
                r.json().then(function(body) {
                    var detail = body && body.error ? (' ' + body.error) : '';
                    _surfaceGroupsSaveError('Watchlist groups could not be saved (check group count/size).' + detail);
                }).catch(function() {
                    _surfaceGroupsSaveError('Watchlist groups could not be saved (check group count/size).');
                });
            }
        }).catch(function() { _groupsSaveInFlight = false; });
    }, 500);
}

// Surface a watchlist-groups failure to the user via the app's existing
// toast (showToast, shared-utils.js). Guarded because watchlist.js loads (and can
// run this) before shared-utils.js has defined showToast.
function _surfaceGroupsSaveError(msg) {
    if (typeof window.showToast === 'function') {
        window.showToast(msg, 4000);
    }
}

// pagehide flush: closing/navigating within the 500ms debounce
// window loses the edit (server-wins hydration reverts it next load).
// keepalive lets the request outlive page teardown; guarded so this only
// binds once even if watchlist.js were ever re-executed.
if (!window._wlPagehideBound) {
    window._wlPagehideBound = true;
    window.addEventListener('pagehide', function() {
        // Fire when a save is pending OR still in flight: a non-keepalive
        // fetch can be cancelled by navigation, and a duplicate full-state
        // save is idempotent — better a double-post than a lost edit.
        if (!_groupsSaveTimer && !_groupsSaveInFlight) return;
        if (_groupsSaveTimer) clearTimeout(_groupsSaveTimer);
        _groupsSaveTimer = null;
        // No toast: the tab is unloading (pagehide), so there's no UI left to
        // show one to. Beacon instead — this is the pending-edit-lost class.
        fetch('/api/watchlist/groups', {
            method: 'POST',
            credentials: 'same-origin',
            keepalive: true,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({ groups: watchlistGroups })
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('watchlist', 'pagehide groups save failed — pending edit lost', String((e && e.message) || e));
            if (typeof Sentry !== 'undefined' && Sentry.captureMessage) {
                Sentry.captureMessage('watchlist groups save failed (pagehide)', { level: 'warning', extra: { err: String((e && e.message) || e) } });
            }
        });
    });
}

function _uniqueGroupId() {
    var base = 'grp_' + Date.now();
    var id = base;
    var n = 1;
    while (watchlistGroups.some(function(g) { return g.id === id; })) {
        id = base + '_' + n;
        n++;
    }
    return id;
}

function createAssetGroup(name) {
    if (watchlistGroups.length >= 50) {
        _surfaceGroupsSaveError('Maximum 50 groups.');
        return;
    }
    var groupName = name || ('Group ' + (watchlistGroups.length + 1));
    groupName = groupName.slice(0, 100);
    watchlistGroups.push({ id: _uniqueGroupId(), name: groupName, symbols: [] });
    saveWatchlistGroups();
}

function renameAssetGroup(id, name) {
    watchlistGroups.forEach(function(g) { if (g.id === id && name) g.name = name.slice(0, 100); });
    saveWatchlistGroups();
}

function deleteAssetGroup(id) {
    if (watchlistGroups.length <= 1) return;
    var victim = null;
    watchlistGroups = watchlistGroups.filter(function(g) {
        if (g.id === id) { victim = g; return false; }
        return true;
    });
    if (victim && victim.symbols.length) {
        // no data loss: orphaned symbols move to the first remaining group
        victim.symbols.forEach(function(s) {
            if (watchlistGroups[0].symbols.indexOf(s) === -1) watchlistGroups[0].symbols.push(s);
        });
    }
    saveWatchlistGroups();
}

function moveSymbolToGroup(symbol, groupId) {
    symbol = symbol.toUpperCase();
    watchlistGroups.forEach(function(g) {
        var i = g.symbols.indexOf(symbol);
        if (i !== -1 && g.id !== groupId) g.symbols.splice(i, 1);
    });
    watchlistGroups.forEach(function(g) {
        if (g.id === groupId && g.symbols.indexOf(symbol) === -1) g.symbols.push(symbol);
    });
    saveWatchlistGroups();
}

function reorderWithinGroup(groupId, fromIdx, toIdx) {
    watchlistGroups.forEach(function(g) {
        if (g.id !== groupId) return;
        var s = g.symbols.splice(fromIdx, 1)[0];
        g.symbols.splice(toIdx, 0, s);
    });
    saveWatchlistGroups();
}

function loadWatchlistSymbols() {
    // Quick load from localStorage for fast render
    var local = _loadLocalWatchlist();
    if (local.length > 0) {
        _watchlistLoaded = true;
    }
    // Async fetch from server (authoritative)
    _fetchWatchlistFromServer(local);
    return local;
}

function _loadLocalWatchlist() {
    // Try new grouped format first (the source of truth)
    var storedGroups = localStorage.getItem('watchlistGroups');
    if (storedGroups) {
        try {
            var gs = JSON.parse(storedGroups);
            if (Array.isArray(gs) && gs.length) {
                watchlistGroups = gs;
                var flat = [];
                gs.forEach(function(g) { (g.symbols || []).forEach(function(s) { if (flat.indexOf(s) === -1) flat.push(s); }); });
                return flat;
            }
        } catch (e) {}
    }
    // Try new flat format
    var stored = localStorage.getItem('watchlistSymbols');
    if (stored) {
        try {
            var arr = JSON.parse(stored);
            if (Array.isArray(arr)) {
                watchlistGroups = _groupsFromFlat(arr);
                return arr;
            }
        } catch (e) {}
    }
    // Migrate from grouped format
    var grouped = localStorage.getItem('watchlistData');
    if (grouped) {
        try {
            var data = JSON.parse(grouped);
            if (data && data.groups) {
                var symbols = [];
                data.groups.forEach(function(g) {
                    (g.symbols || []).forEach(function(s) {
                        if (symbols.indexOf(s) === -1) symbols.push(s);
                    });
                });
                localStorage.setItem('watchlistSymbols', JSON.stringify(symbols));
                watchlistGroups = _groupsFromFlat(symbols);
                return symbols;
            }
        } catch (e) {}
    }
    // Migrate from legacy array
    var legacy = localStorage.getItem('watchlist');
    if (legacy) {
        try {
            var arr = JSON.parse(legacy);
            if (Array.isArray(arr)) {
                localStorage.setItem('watchlistSymbols', JSON.stringify(arr));
                watchlistGroups = _groupsFromFlat(arr);
                return arr;
            }
        } catch (e) {}
    }
    return [];
}

function _fetchWatchlistFromServer(localSymbols) {
    fetch('/api/watchlist?format=groups', {
        method: 'GET',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
    })
    .then(function(response) {
        if (response.ok) return response.json();
        throw new Error('Server returned ' + response.status);
    })
    .then(function(data) {
        var serverGroups = data && Array.isArray(data.groups) ? data.groups : null;
        if (!serverGroups) return;
        var serverHasSymbols = serverGroups.some(function(g) { return (g.symbols || []).length > 0; });
        if (serverHasSymbols) {
            watchlistGroups = serverGroups;
            deriveWatchlistFlat();
            _saveLocal();
            try { localStorage.setItem('watchlistGroups', JSON.stringify(watchlistGroups)); } catch (e) {}
            _watchlistLoaded = true;
            if (typeof renderFilteredTable === 'function') renderFilteredTable();
            if (typeof updateStarIcons === 'function') updateStarIcons();
        } else if (localSymbols && localSymbols.length > 0 && !_watchlistLoaded) {
            if (!watchlistGroups.length) watchlistGroups = _groupsFromFlat(localSymbols);
            saveWatchlistGroups();
            _watchlistLoaded = true;
        }
    })
    .catch(function(err) {
    });
}

// getCSRFToken provided globally by csrf-utils.js (loaded before this script).

function _saveLocal() {
    localStorage.setItem('watchlistSymbols', JSON.stringify(watchlistSymbols));
}

function addToWatchlist(symbol) {
    symbol = symbol.toUpperCase();
    if (watchlistSymbols.indexOf(symbol) === -1) {
        watchlistSymbols.push(symbol);
        _saveLocal();
        if (!watchlistGroups.length) watchlistGroups = _groupsFromFlat([]);
        if (watchlistGroups[0].symbols.indexOf(symbol) === -1) watchlistGroups[0].symbols.push(symbol);
        try { localStorage.setItem('watchlistGroups', JSON.stringify(watchlistGroups)); } catch (e) {}
        // Use atomic server endpoint
        fetch('/api/watchlist/add', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({ symbol: symbol })
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('watchlist', 'add symbol failed on server — local star shows added but server disagrees', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Could not add ' + symbol + ' to your watchlist — check your connection and try again');
        });
    }
}

function removeFromWatchlist(symbol) {
    symbol = symbol.toUpperCase();
    var idx = watchlistSymbols.indexOf(symbol);
    if (idx !== -1) {
        watchlistSymbols.splice(idx, 1);
        _saveLocal();
        watchlistGroups.forEach(function(g) {
            var gi = g.symbols.indexOf(symbol);
            if (gi !== -1) g.symbols.splice(gi, 1);
        });
        try { localStorage.setItem('watchlistGroups', JSON.stringify(watchlistGroups)); } catch (e) {}
        // Use atomic server endpoint
        fetch('/api/watchlist/remove', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({ symbol: symbol })
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('watchlist', 'remove symbol failed on server — symbol may reappear from server sync', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Could not remove ' + symbol + ' from your watchlist — check your connection and try again');
        });
    }
}

function isInWatchlist(symbol) {
    // Callers pass whatever getAttribute('data-symbol') returned,
    // which is null for any .star rendered without one. Nothing is in the
    // watchlist under no name, so answer that instead of throwing — a throw
    // here escapes its forEach and kills the rest of the render.
    if (!symbol) return false;
    return watchlistSymbols.indexOf(symbol.toUpperCase()) !== -1;
}

function getAllWatchlistSymbols() {
    return watchlistSymbols;
}

// Drag and drop reordering for watchlist rows
var draggedWatchlistIdx = null;
var draggedWatchlistGroup = null;

function initWatchlistDragDrop() {
    var tbody = document.getElementById('table-body');
    if (!tbody) return;

    tbody.addEventListener('dragstart', function(e) {
        var row = e.target.closest('tr[data-watchlist-idx]');
        if (!row) return;
        draggedWatchlistIdx = parseInt(row.getAttribute('data-watchlist-idx'));
        draggedWatchlistGroup = row.getAttribute('data-watchlist-group') || 'default';
        row.classList.add('watchlist-dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', draggedWatchlistIdx);
    });

    tbody.addEventListener('dragend', function(e) {
        var row = e.target.closest('tr[data-watchlist-idx]');
        if (row) row.classList.remove('watchlist-dragging');
        tbody.querySelectorAll('.watchlist-drop-above, .watchlist-drop-below').forEach(function(el) {
            el.classList.remove('watchlist-drop-above', 'watchlist-drop-below');
        });
        draggedWatchlistIdx = null;
        draggedWatchlistGroup = null;
    });

    tbody.addEventListener('dragover', function(e) {
        if (draggedWatchlistIdx === null) return;
        var row = e.target.closest('tr[data-watchlist-idx]');
        if (!row) return;
        var rowGroup = row.getAttribute('data-watchlist-group') || 'default';
        if (rowGroup !== draggedWatchlistGroup) return; // cross-group drag deferred by spec
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        tbody.querySelectorAll('.watchlist-drop-above, .watchlist-drop-below').forEach(function(el) {
            el.classList.remove('watchlist-drop-above', 'watchlist-drop-below');
        });
        var rect = row.getBoundingClientRect();
        var midY = rect.top + rect.height / 2;
        if (e.clientY < midY) {
            row.classList.add('watchlist-drop-above');
        } else {
            row.classList.add('watchlist-drop-below');
        }
    });

    tbody.addEventListener('drop', function(e) {
        if (draggedWatchlistIdx === null) return;
        var row = e.target.closest('tr[data-watchlist-idx]');
        if (!row) return;
        var rowGroup = row.getAttribute('data-watchlist-group') || 'default';
        if (rowGroup !== draggedWatchlistGroup) return; // cross-group drag deferred by spec
        e.preventDefault();
        var toIdx = parseInt(row.getAttribute('data-watchlist-idx'));
        if (draggedWatchlistIdx === toIdx) return;
        var rect = row.getBoundingClientRect();
        var midY = rect.top + rect.height / 2;
        var insertBefore = e.clientY < midY;
        var newIdx = insertBefore ? toIdx : toIdx + 1;
        if (draggedWatchlistIdx < toIdx) newIdx--;
        if (watchlistGroups.length > 0) {
            reorderWithinGroup(draggedWatchlistGroup, draggedWatchlistIdx, newIdx);
        } else {
            // defensive legacy path: no groups initialized yet
            var symbol = watchlistSymbols.splice(draggedWatchlistIdx, 1)[0];
            watchlistSymbols.splice(newIdx, 0, symbol);
            _saveLocal();
            // Persist reorder to server
            fetch('/api/watchlist/reorder', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                body: JSON.stringify(watchlistSymbols)
            }).catch(function(e) {
                if (typeof window.carracLog === 'function') window.carracLog('watchlist', 'reorder save failed — server order now stale vs local', String((e && e.message) || e));
                if (typeof showToast === 'function') showToast('Watchlist order could not be saved — check your connection and try again');
            });
        }
        renderFilteredTable();
        if (typeof updateStarIcons === 'function') updateStarIcons();
    });
}
