// Indicator preferences: which indicators are on, favourites and saved
// indicator groups (localStorage first, mirrored to /api/indicator-prefs),
// the favourite pills and group editor, plus the watchlist group editor.
// Needs indicator-registry.js and app-core.js loaded first.

var _indicatorPrefsLoadedFromLocal = false;
var klineActiveIndicators = (function() {
    var defaults = IndicatorRegistry.getDefaults();
    try {
        var saved = JSON.parse(localStorage.getItem('klineActiveIndicators'));
        if (saved) { _indicatorPrefsLoadedFromLocal = true; for (var k in defaults) { if (k in saved) defaults[k] = saved[k]; } }
    } catch(e) {}
    // Guard: if ALL indicators are false (e.g. user pressed I then refreshed), restore defaults
    var anyOn = false;
    for (var k in defaults) { if (defaults[k]) { anyOn = true; break; } }
    if (!anyOn) {
        // Default-on fallback: pull from the indicator registry's declared
        // defaults (each indicator's defaultEnabled flag) instead of
        // hardcoding RSI + VOL. Registry is the single source of truth.
        var reg = (typeof window !== 'undefined' && window.IndicatorRegistry) || null;
        var registryDefaults = null;
        if (reg && typeof reg.getDefaults === 'function') {
            try { registryDefaults = reg.getDefaults(); } catch (e) { /* fall through */ }
        }
        if (registryDefaults) {
            for (var rk in registryDefaults) {
                if (registryDefaults[rk]) defaults[rk] = true;
            }
        } else {
            // Safety net if registry isn't loaded yet.
            defaults.RSI = true;
            defaults.VOL = true;
        }
        try { localStorage.setItem('klineActiveIndicators', JSON.stringify(defaults)); } catch(e) {}
    }
    return defaults;
})();
var klineIndicatorsHidden = false;  // Master indicator visibility toggle
var klineHiddenIndicatorState = (function() {
    try { return JSON.parse(localStorage.getItem('klineHiddenIndicatorState')); } catch(e) { return null; }
})();  // Saved state when hiding all (persisted so I toggle survives refresh)

// Favourite indicators (shown as pills in toolbar)
var _favouritesLoadedFromLocal = false;
var klineFavouriteIndicators = (function() {
    try {
        var saved = JSON.parse(localStorage.getItem('klineFavouriteIndicators'));
        if (saved && saved.length) { _favouritesLoadedFromLocal = true; return saved; }
        return [];
    } catch(e) { return []; }
})();

// Indicator groups (named presets of favourite indicators)
var _groupsLoadedFromLocal = false;
var klineIndicatorGroups = (function() {
    try {
        var s = JSON.parse(localStorage.getItem('klineIndicatorGroups'));
        if (s && Array.isArray(s) && s.length) {
            // Migrate: ensure all groups have explicit active boolean
            s.forEach(function(g) { if (typeof g.active !== 'boolean') g.active = false; });
            _groupsLoadedFromLocal = true;
            return s;
        }
    } catch(e) {}
    return [];
})();
function saveIndicatorGroups() {
    try { localStorage.setItem('klineIndicatorGroups', JSON.stringify(klineIndicatorGroups)); } catch(e) {}
    try {
        fetch('/api/indicator-prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken(), 'X-Sync-Session': (window.DrawingTools && DrawingTools.SYNC_SESSION_ID) || '' },
            credentials: 'same-origin',
            body: JSON.stringify({ groups: klineIndicatorGroups })
        });
    } catch(e) {}
}

// Load indicator prefs from server when localStorage is empty (new device)
// Always fetch indicator prefs from server to sync both directions
(function() {
    fetch('/api/indicator-prefs', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data) return;
            var changed = false;
            // Restore active indicators from server if localStorage was empty
            if (!_indicatorPrefsLoadedFromLocal && data.indicators && Object.keys(data.indicators).length) {
                for (var k in data.indicators) {
                    if (k in klineActiveIndicators) klineActiveIndicators[k] = data.indicators[k];
                }
                try { localStorage.setItem('klineActiveIndicators', JSON.stringify(klineActiveIndicators)); } catch(e) {}
                changed = true;
            }
            // Restore favourites from server if localStorage was empty
            if (!_favouritesLoadedFromLocal && data.favourites && data.favourites.length) {
                klineFavouriteIndicators.length = 0;
                data.favourites.forEach(function(f) { klineFavouriteIndicators.push(f); });
                try { localStorage.setItem('klineFavouriteIndicators', JSON.stringify(klineFavouriteIndicators)); } catch(e) {}
                changed = true;
            }
            // Restore indicator groups from server if localStorage was empty
            if (!_groupsLoadedFromLocal && data.groups && data.groups.length) {
                klineIndicatorGroups.length = 0;
                data.groups.forEach(function(g) { if (typeof g.active !== 'boolean') g.active = false; klineIndicatorGroups.push(g); });
                try { localStorage.setItem('klineIndicatorGroups', JSON.stringify(klineIndicatorGroups)); } catch(e) {}
                changed = true;
            }
            // Restore indicator param overrides from server if localStorage was empty
            if (!localStorage.getItem('indicatorParamOverrides') && data.params && Object.keys(data.params).length) {
                if (typeof IndicatorSettings !== 'undefined' && IndicatorSettings.applyOverrides) {
                    IndicatorSettings.applyOverrides(data.params);
                }
            }
            // Reverse migration: push local data to server when server has none
            var pushPayload = {};
            if (_indicatorPrefsLoadedFromLocal && (!data.indicators || !Object.keys(data.indicators).length)) {
                pushPayload.indicators = klineActiveIndicators;
            }
            if (_favouritesLoadedFromLocal && (!data.favourites || !data.favourites.length)) {
                pushPayload.favourites = klineFavouriteIndicators;
            }
            if (_groupsLoadedFromLocal && (!data.groups || !data.groups.length)) {
                pushPayload.groups = klineIndicatorGroups;
            }
            if (Object.keys(pushPayload).length > 0) {
                // SILENCE-OK: localStorage (klineActiveIndicators/klineFavouriteIndicators/
                // klineIndicatorGroups) is already the authoritative copy on this device —
                // this is a one-way mirror to the server for cross-device pickup. A miss
                // just means the next device sees the old server state; it retries every
                // time this device boots with local data and empty server data.
                fetch('/api/indicator-prefs', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': (typeof getCSRFToken === 'function' ? getCSRFToken() : '') },
                    credentials: 'same-origin',
                    body: JSON.stringify(pushPayload)
                }).catch(function() {});
            }
            // Re-render UI if chart is already open
            if (changed) {
                if (typeof renderFavouritePills === 'function') renderFavouritePills();
                // Update indicator menu checkboxes if visible
                document.querySelectorAll('.indicator-menu-item[data-ind]').forEach(function(item) {
                    var ind = item.getAttribute('data-ind');
                    var check = item.querySelector('.ind-checkbox');
                    item.classList.toggle('active', !!klineActiveIndicators[ind]);
                    if (check) check.classList.toggle('active', !!klineActiveIndicators[ind]);
                });
            }
        })
        // SILENCE-OK: local defaults (klineActiveIndicators etc.) are already
        // in effect before this fetch even starts — a miss here just means
        // this device doesn't pick up another device's prefs this load; the
        // local set stays usable and this GET retries on the next page load.
        .catch(function() {});
})();
var klineIndicatorLabels = IndicatorRegistry.getLabels();
function saveFavouriteIndicators() {
    localStorage.setItem('klineFavouriteIndicators', JSON.stringify(klineFavouriteIndicators));
    try {
        fetch('/api/indicator-prefs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken(), 'X-Sync-Session': (window.DrawingTools && DrawingTools.SYNC_SESSION_ID) || '' },
            credentials: 'same-origin',
            body: JSON.stringify({ favourites: klineFavouriteIndicators })
        });
    } catch(e) {}
}
function toggleFavouriteIndicator(key) {
    var idx = klineFavouriteIndicators.indexOf(key);
    if (idx >= 0) klineFavouriteIndicators.splice(idx, 1);
    else klineFavouriteIndicators.push(key);
    saveFavouriteIndicators();
    renderFavouritePills();
    // Update star icons in dropdown
    document.querySelectorAll('.ind-fav-star').forEach(function(s) {
        s.classList.toggle('active', klineFavouriteIndicators.indexOf(s.getAttribute('data-ind')) >= 0);
    });
}
// Group "active" is DERIVED from the live indicator state — a pill is lit only
// when every member is actually on (partial = some on). The persisted
// group.active flag is kept for server back-compat but never drives the UI.
function _isGroupActive(group) {
    if (!group || !group.indicators || !group.indicators.length) return false;
    return group.indicators.every(function(k) { return !!klineActiveIndicators[k]; });
}
function _isGroupPartial(group) {
    if (!group || !group.indicators || !group.indicators.length) return false;
    if (_isGroupActive(group)) return false;
    return group.indicators.some(function(k) { return !!klineActiveIndicators[k]; });
}
// Cheap class-only refresh of existing group pills (desktop bar + mobile sheet)
// — called from toggleKlineIndicator so per-indicator toggles can never leave a
// stale group pill behind.
function _refreshGroupPillStates() {
    klineIndicatorGroups.forEach(function(group) {
        var active = _isGroupActive(group);
        var partial = _isGroupPartial(group);
        group.active = active;  // keep the persisted flag honest
        ['.ind-fav-pill.ind-fav-group[data-group-id="' + group.id + '"]',
         '.mobile-ind-group-pill[data-group-id="' + group.id + '"]'].forEach(function(sel) {
            document.querySelectorAll(sel).forEach(function(pill) {
                pill.classList.toggle('active', active);
                pill.classList.toggle('partial', partial);
            });
        });
    });
}

function renderFavouritePills() {
    var container = document.getElementById('ind-fav-pills');
    if (!container) return;
    container.innerHTML = '';

    // Group pills first
    klineIndicatorGroups.forEach(function(group) {
        var wrapper = document.createElement('span');
        wrapper.className = 'ind-fav-group-wrapper';

        var pill = document.createElement('button');
        pill.className = 'ind-fav-pill ind-fav-group' + (_isGroupActive(group) ? ' active' : '') + (_isGroupPartial(group) ? ' partial' : '');
        pill.setAttribute('data-group-id', group.id);
        pill.textContent = group.name;
        pill.title = group.name + (group.indicators.length ? ': ' + group.indicators.map(function(k) { return klineIndicatorLabels[k] || k; }).join(', ') : ' (empty)');
        pill.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleIndicatorGroup(group.id);
        });
        wrapper.appendChild(pill);

        var editZone = document.createElement('button');
        editZone.className = 'ind-group-edit-zone';
        editZone.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';
        editZone.title = 'Edit group';
        editZone.addEventListener('click', function(e) {
            e.stopPropagation();
            try {
                showGroupEditor(group.id);
            } catch(err) {
                console.error('[GROUP] showGroupEditor error:', err);
            }
        });
        wrapper.appendChild(editZone);
        container.appendChild(wrapper);
    });

    // Individual favourite pills
    klineFavouriteIndicators.forEach(function(key) {
        var pill = document.createElement('button');
        pill.className = 'ind-fav-pill' + (klineActiveIndicators[key] ? ' active' : '');
        pill.setAttribute('data-ind', key);
        pill.textContent = klineIndicatorLabels[key] || key;
        pill.title = key;
        pill.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleKlineIndicator(key);
            pill.classList.toggle('active', !!klineActiveIndicators[key]);
        });
        container.appendChild(pill);
    });

    // "+" button to create new group
    var addBtn = document.createElement('button');
    addBtn.className = 'ind-fav-pill ind-group-add';
    addBtn.textContent = '+';
    addBtn.title = 'Create indicator group';
    addBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        _createAndEditGroup();
    });
    container.appendChild(addBtn);
}

function toggleIndicatorGroup(groupId) {
    var group = klineIndicatorGroups.find(function(g) { return g.id === groupId; });
    if (!group || !group.indicators.length) return;
    var turningOn = !_isGroupActive(group);
    group.active = turningOn;
    if (turningOn) {
        // Activate VOL first (if in group) so its pane is created before others
        var sorted = group.indicators.slice().sort(function(a, b) {
            return (a === 'VOL' ? -1 : b === 'VOL' ? 1 : 0);
        });
        sorted.forEach(function(k) {
            if (!klineActiveIndicators[k]) toggleKlineIndicator(k);
        });
    } else {
        // Deactivate members — but keep indicators that belong to another active group
        // Deactivate VOL last so other panes are removed first
        var protectedKeys = {};
        klineIndicatorGroups.forEach(function(g) {
            if (g.id !== groupId && _isGroupActive(g)) {
                g.indicators.forEach(function(k) { protectedKeys[k] = true; });
            }
        });
        var sortedOff = group.indicators.slice().sort(function(a, b) {
            return (a === 'VOL' ? 1 : b === 'VOL' ? -1 : 0);
        });
        sortedOff.forEach(function(k) {
            if (!protectedKeys[k] && klineActiveIndicators[k]) toggleKlineIndicator(k);
        });
    }
    saveIndicatorGroups();
    renderFavouritePills();
}

function _createAndEditGroup() {
    var n = klineIndicatorGroups.length + 1;
    var group = { id: 'grp_' + Date.now(), name: 'Group ' + n, indicators: [], active: false };
    klineIndicatorGroups.push(group);
    saveIndicatorGroups();
    renderFavouritePills();
    // Small delay so DOM has the new pill before positioning
    setTimeout(function() { showGroupEditor(group.id, true); }, 20);
}

function showGroupEditor(groupId, isNew) {
    var group = klineIndicatorGroups.find(function(g) { return g.id === groupId; });
    if (!group) { console.warn('[GROUP] Group not found!'); return; }

    // Close any existing editor
    var existing = document.getElementById('group-editor-popup');
    if (existing) existing.remove();

    var popup = document.createElement('div');
    popup.id = 'group-editor-popup';
    popup.className = 'group-editor-popup';
    popup.addEventListener('click', function(e) { e.stopPropagation(); });

    // Name input
    var nameRow = document.createElement('div');
    nameRow.className = 'group-editor-name-row';
    var nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'group-editor-name';
    nameInput.value = group.name;
    nameInput.placeholder = 'Group name';
    nameInput.maxLength = 30;
    nameInput.addEventListener('input', function() {
        group.name = this.value.trim() || 'Untitled';
        saveIndicatorGroups();
        // Update pill text live
        var pill = document.querySelector('.ind-fav-group[data-group-id="' + groupId + '"]');
        if (pill) pill.textContent = group.name;
    });
    nameInput.addEventListener('keydown', function(e) {
        e.stopPropagation();
        if (e.key === 'Enter') { nameInput.blur(); }
        if (e.key === 'Escape') { _closeGroupEditor(); }
    });
    nameRow.appendChild(nameInput);
    popup.appendChild(nameRow);

    // Indicator checklist — all indicators, grouped by category
    var indCategories = IndicatorRegistry.getCategoryOrder();
    var list = document.createElement('div');
    list.className = 'group-editor-list';
    indCategories.forEach(function(cat) {
        var hdr = document.createElement('div');
        hdr.className = 'group-editor-category';
        hdr.textContent = cat.label;
        list.appendChild(hdr);
        cat.keys.forEach(function(key) {
            if (!klineIndicatorLabels[key]) return;
            var row = document.createElement('label');
            row.className = 'group-editor-item';
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = group.indicators.indexOf(key) >= 0;
            cb.addEventListener('change', function() {
                var idx = group.indicators.indexOf(key);
                if (this.checked && idx < 0) group.indicators.push(key);
                else if (!this.checked && idx >= 0) group.indicators.splice(idx, 1);
                saveIndicatorGroups();
                // Only toggle the indicator on the chart if this group is active
                if (group.active) {
                    var shouldBeOn = this.checked;
                    // When unchecking, only turn off if no other active group uses this indicator
                    if (!shouldBeOn && klineActiveIndicators[key]) {
                        var protectedByOther = klineIndicatorGroups.some(function(g) {
                            return g.id !== group.id && g.active && g.indicators.indexOf(key) >= 0;
                        });
                        if (!protectedByOther) toggleKlineIndicator(key);
                    } else if (shouldBeOn && !klineActiveIndicators[key]) {
                        toggleKlineIndicator(key);
                    }
                }
                // Update pill tooltip (keeps editor open)
                var pill = document.querySelector('.ind-fav-group[data-group-id="' + groupId + '"]');
                if (pill) {
                    pill.title = group.name + (group.indicators.length ? ': ' + group.indicators.map(function(k) { return klineIndicatorLabels[k] || k; }).join(', ') : ' (empty)');
                }
            });
            row.appendChild(cb);
            var label = document.createElement('span');
            label.textContent = klineIndicatorLabels[key] || key;
            row.appendChild(label);
            list.appendChild(row);
        });
    });
    popup.appendChild(list);

    // Delete button
    var footer = document.createElement('div');
    footer.className = 'group-editor-footer';
    var delBtn = document.createElement('button');
    delBtn.className = 'group-editor-delete';
    delBtn.textContent = 'Delete Group';
    delBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        klineIndicatorGroups = klineIndicatorGroups.filter(function(g) { return g.id !== groupId; });
        saveIndicatorGroups();
        _closeGroupEditor();
        renderFavouritePills();
    });
    footer.appendChild(delBtn);
    popup.appendChild(footer);

    document.body.appendChild(popup);

    // Position near the group pill or "+" button
    var anchor = document.querySelector('.ind-fav-group[data-group-id="' + groupId + '"]')
        || document.querySelector('.mobile-ind-group-pill[data-group-id="' + groupId + '"]')
        || document.querySelector('.ind-group-add')
        || document.querySelector('.mobile-ind-group-add');
    if (anchor) {
        var rect = anchor.getBoundingClientRect();
        var popW = 220;
        if (window.innerWidth <= 768) {
            // Mobile: center horizontally, position below anchor but clamp to viewport
            popup.style.left = Math.max(8, (window.innerWidth - popW) / 2) + 'px';
            popup.style.right = Math.max(8, (window.innerWidth - popW) / 2) + 'px';
            popup.style.top = Math.min(rect.bottom + 6, window.innerHeight * 0.15) + 'px';
            popup.style.maxHeight = '60vh';
        } else {
            var left = Math.min(rect.left, window.innerWidth - popW - 10);
            popup.style.left = Math.max(4, left) + 'px';
            popup.style.top = (rect.bottom + 6) + 'px';
        }
    }

    // Auto-focus and select name for new groups
    if (isNew) {
        nameInput.focus();
        nameInput.select();
    }

    // Close on click outside (delayed to avoid catching the triggering click)
    setTimeout(function() {
        document.addEventListener('click', _groupEditorOutsideClick);
    }, 50);
}

function _groupEditorOutsideClick(e) {
    var popup = document.getElementById('group-editor-popup');
    if (popup && !popup.contains(e.target)) {
        _closeGroupEditor();
    }
}

function _closeGroupEditor() {
    var popup = document.getElementById('group-editor-popup');
    if (popup) popup.remove();
    document.removeEventListener('click', _groupEditorOutsideClick);
    renderFavouritePills();
    // If mobile indicator sheet is open, rebuild it to reflect group changes
    var mobileSheet = document.getElementById('mobile-ind-sheet');
    if (mobileSheet) { mobileSheet.remove(); openMobileIndicatorSheet(); }
}

// ── Asset (watchlist) group editor ──────────────────────────────
function showAssetGroupEditor() {
    var pop = document.getElementById('asset-group-editor');
    if (!pop) return;
    if (pop.style.display === 'block') { closeAssetGroupEditor(); return; }
    if (typeof watchlistGroups !== 'undefined' && !watchlistGroups.length && watchlistSymbols.length) {
        watchlistGroups = _groupsFromFlat(watchlistSymbols);
    }
    _renderAssetGroupEditor();
    var btn = document.getElementById('groups-edit-btn');
    var r = btn ? btn.getBoundingClientRect() : { right: window.innerWidth - 20, bottom: 60 };
    pop.style.display = 'block';
    pop.style.top = (r.bottom + 6) + 'px';
    pop.style.left = Math.max(8, r.right - 260) + 'px';
    setTimeout(function() { document.addEventListener('click', _ageOutsideClick); }, 0);
}

function closeAssetGroupEditor() {
    var pop = document.getElementById('asset-group-editor');
    if (pop) pop.style.display = 'none';
    document.removeEventListener('click', _ageOutsideClick);
}

function _ageOutsideClick(e) {
    var pop = document.getElementById('asset-group-editor');
    if (pop && !pop.contains(e.target) && e.target.id !== 'groups-edit-btn') closeAssetGroupEditor();
}

function _geAddSymbol(input) {
    var gid = input.getAttribute('data-ge-add');
    var sym = input.value.trim().toUpperCase();
    if (!sym) return;
    if (!/^[A-Za-z0-9.=_-]{1,30}$/.test(sym)) {
        _surfaceGroupsSaveError('Invalid symbol.');
        return;
    }
    var g = null;
    for (var i = 0; i < watchlistGroups.length; i++) {
        if (watchlistGroups[i].id === gid) { g = watchlistGroups[i]; break; }
    }
    if (g && g.symbols.length >= 100 && g.symbols.indexOf(sym) === -1) {
        _surfaceGroupsSaveError('Group is full (100 symbols max).');
        return;
    }
    // Clear now (not after re-render) so a same-tick duplicate event on this
    // same input node (Enter following a datalist-pick 'change', or vice
    // versa) reads an empty value and no-ops instead of adding twice.
    input.value = '';
    moveSymbolToGroup(sym, gid);
    _renderAssetGroupEditor();
    renderFilteredTable();
    if (typeof updateStarIcons === 'function') updateStarIcons();
}

function _renderAssetGroupEditor() {
    var pop = document.getElementById('asset-group-editor');
    if (!pop) return;
    var html = '';
    watchlistGroups.forEach(function(g) {
        html += '<div class="ge-group" data-gid="' + escapeHtml(g.id) + '">' +
            '<div class="ge-group-head">' +
            '<input type="text" value="' + escapeHtml(g.name) + '" maxlength="100" data-ge-rename="' + escapeHtml(g.id) + '">' +
            (watchlistGroups.length > 1 ? '<button class="ge-del" data-ge-del="' + escapeHtml(g.id) + '" title="Delete group (symbols move to your first group)">&#10005;</button>' : '') +
            '</div>';
        watchlistSymbols.forEach(function(s) {
            var inG = g.symbols.indexOf(s) !== -1;
            html += '<label><input type="checkbox"' + (inG ? ' checked' : '') +
                ' data-ge-sym="' + escapeHtml(s) + '" data-ge-gid="' + escapeHtml(g.id) + '">' + escapeHtml(s) + '</label>';
        });
        html += '<div class="ge-group-head">' +
            '<input type="text" class="ge-add-input" list="ge-symbol-datalist" placeholder="Add symbol…" maxlength="30" data-ge-add="' + escapeHtml(g.id) + '">' +
            '</div>';
        html += '</div>';
    });
    html += '<button class="ge-new-btn" data-ge-new="1">+ New group</button>';
    if (typeof allAssets !== 'undefined') {
        html += '<datalist id="ge-symbol-datalist">';
        Object.keys(allAssets).sort().forEach(function(sym) {
            html += '<option value="' + escapeHtml(sym) + '">';
        });
        html += '</datalist>';
    }
    pop.innerHTML = html;

    pop.querySelectorAll('input[data-ge-rename]').forEach(function(inp) {
        inp.addEventListener('change', function() {
            var gid = inp.getAttribute('data-ge-rename');
            var trimmed = inp.value.trim();
            if (!trimmed) {
                // Blank rename is a no-op (renameAssetGroup ignores falsy names) —
                // reset the visible input back to the group's unchanged name.
                var g = watchlistGroups.filter(function(x) { return x.id === gid; })[0];
                inp.value = g ? g.name : inp.value;
                return;
            }
            renameAssetGroup(gid, trimmed);
            renderFilteredTable();
        });
    });
    pop.querySelectorAll('button[data-ge-del]').forEach(function(btn) {
        btn.addEventListener('click', function(e) { e.stopPropagation(); deleteAssetGroup(btn.getAttribute('data-ge-del')); _renderAssetGroupEditor(); renderFilteredTable(); });
    });
    pop.querySelectorAll('input[data-ge-sym]').forEach(function(cb) {
        cb.addEventListener('change', function() {
            var sym = cb.getAttribute('data-ge-sym');
            if (cb.checked) {
                moveSymbolToGroup(sym, cb.getAttribute('data-ge-gid'));   // single membership: move
            } else {
                moveSymbolToGroup(sym, watchlistGroups[0].id);            // uncheck = back to first group
            }
            _renderAssetGroupEditor();
            renderFilteredTable();
        });
    });
    var nb = pop.querySelector('button[data-ge-new]');
    if (nb) nb.addEventListener('click', function(e) { e.stopPropagation(); createAssetGroup(''); _renderAssetGroupEditor(); renderFilteredTable(); });
    pop.querySelectorAll('input[data-ge-add]').forEach(function(inp) {
        // Enter-only submission — deliberately NO 'change' listener: change
        // also fires on any dirty blur (e.g. the innerHTML swap when another
        // popup control re-renders), which would silently commit stray text
        // as a symbol and re-enter this render mid-swap. A datalist pick
        // just fills the value; Enter confirms it.
        inp.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); _geAddSymbol(inp); }
        });
    });
}
