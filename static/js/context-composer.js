/**
 * Context Composer — visual block-based recipe builder for AI context.
 *
 * Users select, configure, and prioritize data blocks that feed into AI queries.
 * Recipes are saved server-side and synced to localStorage.
 */

var _composerOpen = false;
var _composerBlocks = [];    // available blocks from server
var _composerRecipes = [];   // user's saved recipes
var _composerTemplates = []; // built-in templates
var _composerActiveRecipe = null;  // currently loaded recipe
var _composerRecipeId = null;      // active recipe ID (null = legacy mode)
var _composerDirty = false;
var _composerDragItem = null;

// Thesis text persisted in localStorage
var _composerThesis = localStorage.getItem('composerThesis') || '';

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function initContextComposer() {
    var btn = document.getElementById('ai-context-btn');
    if (btn) btn.addEventListener('click', toggleComposer);

    var backBtn = document.getElementById('composer-back');
    if (backBtn) backBtn.addEventListener('click', toggleComposer);

    var saveBtn = document.getElementById('composer-save');
    if (saveBtn) saveBtn.addEventListener('click', composerSave);

    var resetBtn = document.getElementById('composer-reset');
    if (resetBtn) resetBtn.addEventListener('click', composerReset);

    var previewBtn = document.getElementById('composer-preview-btn');
    if (previewBtn) previewBtn.addEventListener('click', composerPreview);

    var templateSel = document.getElementById('composer-template');
    if (templateSel) templateSel.addEventListener('change', composerSelectTemplate);

    // Thesis input
    var thesisInput = document.getElementById('composer-thesis-input');
    if (thesisInput) {
        thesisInput.value = _composerThesis;
        thesisInput.addEventListener('input', function() {
            _composerThesis = this.value;
            localStorage.setItem('composerThesis', _composerThesis);
            _composerUpdateBudgetBar();
        });
    }

    // Load saved recipe ID
    var saved = localStorage.getItem('composerRecipeId');
    if (saved) _composerRecipeId = parseInt(saved);

    // Load data from server
    _composerLoadBlocks();
    _composerLoadRecipes();
    _composerLoadTemplates();
}

// ---------------------------------------------------------------------------
// Toggle
// ---------------------------------------------------------------------------

function toggleComposer() {
    _composerOpen = !_composerOpen;
    var panel = document.getElementById('composer-panel');
    var chat = document.getElementById('ai-panel-chat');
    var input = document.getElementById('ai-panel-input-wrapper');
    var headerRight = document.getElementById('ai-panel-header-right');

    if (panel) panel.style.display = _composerOpen ? 'flex' : 'none';
    if (chat) chat.style.display = _composerOpen ? 'none' : 'flex';
    if (input) input.style.display = _composerOpen ? 'none' : 'flex';

    // Toggle context button active state
    var btn = document.getElementById('ai-context-btn');
    if (btn) btn.classList.toggle('active', _composerOpen);

    if (_composerOpen) {
        _composerRender();
    }
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

function _composerLoadBlocks() {
    fetch('/api/ai/context-blocks', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (data && data.blocks) _composerBlocks = data.blocks;
        }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/ai/context-blocks failed — rendered as empty', String((e && e.message) || e)); });
}

function _composerLoadRecipes() {
    fetch('/api/ai/recipes', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data || !data.recipes) return;
            _composerRecipes = data.recipes;
            // Load default recipe or first one
            if (_composerRecipeId) {
                var found = _composerRecipes.find(function(r) { return r.id === _composerRecipeId; });
                if (found) { _composerActiveRecipe = found; return; }
            }
            var def = _composerRecipes.find(function(r) { return r.is_default; });
            if (def) {
                _composerActiveRecipe = def;
                _composerRecipeId = def.id;
                localStorage.setItem('composerRecipeId', def.id);
            }
        }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/ai/recipes failed — rendered as empty', String((e && e.message) || e)); });
}

function _composerLoadTemplates() {
    fetch('/api/ai/recipes/templates', { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (data && data.templates) {
                _composerTemplates = data.templates;
                _composerUpdateTemplateDropdown();
            }
        }).catch(function(e) { if (typeof window.carracLog === 'function') window.carracLog('fetch', '/api/ai/recipes/templates failed — template dropdown stays empty', String((e && e.message) || e)); });
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function _composerRender() {
    var list = document.getElementById('composer-block-list');
    if (!list) return;
    list.innerHTML = '';

    // Get blocks from active recipe (or defaults)
    var recipeBlocks = _composerGetRecipeBlocks();

    // Group: enabled blocks (sorted by priority), then disabled
    var enabled = recipeBlocks.filter(function(b) { return b.enabled; })
        .sort(function(a, b) { return a.priority - b.priority; });
    var disabled = recipeBlocks.filter(function(b) { return !b.enabled; });

    enabled.concat(disabled).forEach(function(rb) {
        var blockMeta = _composerBlocks.find(function(b) { return b.id === rb.id; });
        if (!blockMeta) return;
        list.appendChild(_composerCreateCard(rb, blockMeta));
    });

    _composerUpdateBudgetBar();
    _composerUpdateTemplateDropdown();
    _composerSetupDrag(list);
}

function _composerCreateCard(recipeBlock, meta) {
    var card = document.createElement('div');
    card.className = 'composer-card' + (recipeBlock.enabled ? '' : ' disabled') + (meta.locked ? ' locked' : '');
    card.setAttribute('data-block-id', recipeBlock.id);

    // Drag handle
    var handle = document.createElement('span');
    handle.className = 'composer-drag-handle';
    handle.textContent = '\u2261';
    handle.title = 'Drag to reorder';

    // Toggle
    var toggle = document.createElement('button');
    toggle.className = 'composer-toggle';
    toggle.innerHTML = recipeBlock.enabled ? '\u25C9' : '\u25CB';
    toggle.title = recipeBlock.enabled ? 'Disable' : 'Enable';
    if (meta.locked) {
        toggle.disabled = true;
        toggle.title = 'Requires ' + meta.tier_minimum + ' tier';
    }
    toggle.addEventListener('click', function() {
        recipeBlock.enabled = !recipeBlock.enabled;
        _composerDirty = true;
        _composerRender();
        _posthogTrack('context_block_toggled', { block: recipeBlock.id, enabled: recipeBlock.enabled });
    });

    // Name
    var name = document.createElement('span');
    name.className = 'composer-block-name';
    name.textContent = meta.name;
    if (meta.locked) {
        name.innerHTML += ' <span class="composer-lock">\uD83D\uDD12 ' + meta.tier_minimum + '</span>';
    }

    // Token estimate bar
    var tokenBar = document.createElement('div');
    tokenBar.className = 'composer-token-bar';
    var fill = document.createElement('div');
    fill.className = 'composer-token-fill';
    var maxTokens = 600;
    var pct = Math.min(100, (meta.token_estimate / maxTokens) * 100);
    fill.style.width = pct + '%';
    tokenBar.appendChild(fill);
    var tokenLabel = document.createElement('span');
    tokenLabel.className = 'composer-token-label';
    tokenLabel.textContent = '~' + meta.token_estimate + ' tokens';

    // Config area (expandable)
    var configArea = null;
    if (meta.config_schema && Object.keys(meta.config_schema).length > 0 && recipeBlock.enabled) {
        configArea = _composerBuildConfig(recipeBlock, meta);
    }

    // Description tooltip
    card.title = meta.description;

    // Header row
    var header = document.createElement('div');
    header.className = 'composer-card-header';
    header.appendChild(toggle);
    header.appendChild(name);
    header.appendChild(tokenLabel);
    header.appendChild(handle);

    card.appendChild(header);

    if (recipeBlock.enabled && !meta.locked) {
        card.appendChild(tokenBar);
    }

    if (configArea) {
        card.appendChild(configArea);
    }

    return card;
}

function _composerBuildConfig(recipeBlock, meta) {
    var div = document.createElement('div');
    div.className = 'composer-config';

    var schema = meta.config_schema;
    Object.keys(schema).forEach(function(key) {
        var spec = schema[key];
        if (spec.type === 'text') {
            // Skip thesis — it has its own dedicated input
            if (key === 'text' && recipeBlock.id === 'thesis') return;
        }
        if (spec.type === 'number') {
            var label = document.createElement('label');
            label.className = 'composer-config-label';
            label.textContent = spec.label || key;
            var input = document.createElement('input');
            input.type = 'number';
            input.className = 'composer-config-input';
            input.min = spec.min || 0;
            input.max = spec.max || 1000;
            input.value = (recipeBlock.config && recipeBlock.config[key]) || spec.default || 0;
            input.addEventListener('change', function() {
                if (!recipeBlock.config) recipeBlock.config = {};
                recipeBlock.config[key] = parseInt(this.value);
                _composerDirty = true;
            });
            div.appendChild(label);
            div.appendChild(input);
        }
        if (spec.type === 'multi_select') {
            var label = document.createElement('label');
            label.className = 'composer-config-label';
            label.textContent = spec.label || key;
            div.appendChild(label);
            var chips = document.createElement('div');
            chips.className = 'composer-chips';
            var selected = (recipeBlock.config && recipeBlock.config[key]) || spec.default || [];
            (spec.options || []).forEach(function(opt) {
                var chip = document.createElement('button');
                chip.className = 'composer-chip' + (selected.indexOf(opt) !== -1 ? ' active' : '');
                chip.textContent = opt;
                chip.addEventListener('click', function() {
                    if (!recipeBlock.config) recipeBlock.config = {};
                    var arr = recipeBlock.config[key] || [].concat(spec.default || []);
                    var idx = arr.indexOf(opt);
                    if (idx !== -1) arr.splice(idx, 1); else arr.push(opt);
                    recipeBlock.config[key] = arr;
                    this.classList.toggle('active');
                    _composerDirty = true;
                });
                chips.appendChild(chip);
            });
            div.appendChild(chips);
        }
    });

    return div.children.length > 0 ? div : null;
}

// ---------------------------------------------------------------------------
// Budget bar
// ---------------------------------------------------------------------------

function _composerUpdateBudgetBar() {
    var bar = document.getElementById('composer-budget-fill');
    var label = document.getElementById('composer-budget-label');
    var tierLabel = document.getElementById('composer-tier-label');
    if (!bar || !label) return;

    var recipeBlocks = _composerGetRecipeBlocks();
    var total = 0;
    recipeBlocks.forEach(function(rb) {
        if (!rb.enabled) return;
        var meta = _composerBlocks.find(function(b) { return b.id === rb.id; });
        if (meta && !meta.locked) total += meta.token_estimate;
    });

    // Add thesis estimate
    if (_composerThesis) total += Math.ceil(_composerThesis.length / 4);

    var budget = 4000; // free tier stub
    var pct = Math.min(100, (total / budget) * 100);
    bar.style.width = pct + '%';
    bar.className = 'composer-budget-bar-fill' + (pct > 90 ? ' over' : pct > 70 ? ' warn' : '');
    label.textContent = total + '/' + budget + ' tokens';
    if (tierLabel) tierLabel.textContent = 'Tier budget: ' + budget.toLocaleString() + ' tokens (Free)';
}

// ---------------------------------------------------------------------------
// Template selector
// ---------------------------------------------------------------------------

function _composerUpdateTemplateDropdown() {
    var sel = document.getElementById('composer-template');
    if (!sel) return;

    // Preserve current selection
    var current = sel.value;
    sel.innerHTML = '<option value="">Custom</option>';

    _composerTemplates.forEach(function(t) {
        var opt = document.createElement('option');
        opt.value = t.id;
        opt.textContent = t.name + ' (' + t.block_count + ' blocks)';
        sel.appendChild(opt);
    });

    // Also add user's saved recipes
    _composerRecipes.forEach(function(r) {
        var opt = document.createElement('option');
        opt.value = 'recipe_' + r.id;
        opt.textContent = r.name + (r.is_default ? ' (default)' : '');
        sel.appendChild(opt);
    });

    if (current) sel.value = current;
}

function composerSelectTemplate() {
    var sel = document.getElementById('composer-template');
    if (!sel || !sel.value) return;

    var val = sel.value;
    if (val.indexOf('recipe_') === 0) {
        // Load saved recipe
        var rid = parseInt(val.replace('recipe_', ''));
        var recipe = _composerRecipes.find(function(r) { return r.id === rid; });
        if (recipe) {
            _composerActiveRecipe = recipe;
            _composerRecipeId = recipe.id;
            localStorage.setItem('composerRecipeId', recipe.id);
            _composerDirty = false;
            _composerRender();
        }
    } else {
        // Clone from template
        var csrf = _getCSRF();
        fetch('/api/ai/recipes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            credentials: 'same-origin',
            body: JSON.stringify({ template_id: val, is_default: true }),
        }).then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.id) {
                _composerRecipeId = data.id;
                localStorage.setItem('composerRecipeId', data.id);
                _composerLoadRecipes();
                // Reload after recipes refresh
                setTimeout(function() {
                    var recipe = _composerRecipes.find(function(r) { return r.id === _composerRecipeId; });
                    if (recipe) _composerActiveRecipe = recipe;
                    _composerRender();
                }, 500);
                _posthogTrack('context_template_selected', { template: val });
            }
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('composer', 'clone-from-template failed — recipe not created', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Template could not be applied — check your connection and try again');
        });
    }
}

// ---------------------------------------------------------------------------
// Save / Reset
// ---------------------------------------------------------------------------

function composerSave() {
    var recipeBlocks = _composerGetRecipeBlocks();
    var csrf = _getCSRF();

    if (_composerRecipeId) {
        // Update existing
        fetch('/api/ai/recipes/' + _composerRecipeId, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            credentials: 'same-origin',
            body: JSON.stringify({ blocks: recipeBlocks, is_default: true }),
        }).then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.ok) {
                _composerDirty = false;
                _composerLoadRecipes();
                _posthogTrack('context_recipe_created', { action: 'update' });
            }
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('composer', 'recipe update save failed — Save button silently no-op\'d', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Context could not be saved — check your connection and try again');
        });
    } else {
        // Create new
        fetch('/api/ai/recipes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            credentials: 'same-origin',
            body: JSON.stringify({ name: 'My Recipe', blocks: recipeBlocks, is_default: true }),
        }).then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.id) {
                _composerRecipeId = data.id;
                localStorage.setItem('composerRecipeId', data.id);
                _composerDirty = false;
                _composerLoadRecipes();
                _posthogTrack('context_recipe_created', { action: 'create' });
            }
        }).catch(function(e) {
            if (typeof window.carracLog === 'function') window.carracLog('composer', 'recipe create save failed — Save button silently no-op\'d', String((e && e.message) || e));
            if (typeof showToast === 'function') showToast('Context could not be saved — check your connection and try again');
        });
    }
}

function composerReset() {
    _composerActiveRecipe = null;
    _composerRecipeId = null;
    localStorage.removeItem('composerRecipeId');
    _composerDirty = false;
    _composerRender();
}

// ---------------------------------------------------------------------------
// Preview
// ---------------------------------------------------------------------------

function composerPreview() {
    var previewEl = document.getElementById('composer-preview-content');
    if (!previewEl) return;

    var visible = previewEl.parentElement.style.display !== 'none';
    previewEl.parentElement.style.display = visible ? 'none' : 'block';
    if (visible) return;

    previewEl.textContent = 'Loading preview...';
    var asset = (typeof klineChartSymbol !== 'undefined' && klineChartSymbol) || 'BTC';
    var tf = (typeof klineChartTimeframe !== 'undefined' && klineChartTimeframe) || '1D';
    var csrf = _getCSRF();

    fetch('/api/ai/context-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        credentials: 'same-origin',
        body: JSON.stringify({
            recipe_id: _composerRecipeId,
            asset: asset,
            timeframe: tf,
            thesis_text: _composerThesis,
        }),
    }).then(function(r) { return r.json(); })
    .then(function(data) {
        var lines = [];
        if (data.blocks) {
            data.blocks.forEach(function(b) {
                lines.push((b.included ? '\u2713 ' : '\u2717 ') + b.name + ' (' + b.tokens + ' tokens)');
            });
        }
        lines.push('\nTotal: ' + data.total_tokens + '/' + data.budget + ' tokens');
        lines.push('\n---\n');
        lines.push(data.text || '(empty)');
        previewEl.textContent = lines.join('\n');
    }).catch(function() {
        previewEl.textContent = 'Preview failed';
    });
}

// ---------------------------------------------------------------------------
// Drag & Drop reorder
// ---------------------------------------------------------------------------

function _composerSetupDrag(list) {
    var cards = list.querySelectorAll('.composer-card:not(.disabled):not(.locked)');
    cards.forEach(function(card) {
        card.setAttribute('draggable', 'true');
        card.addEventListener('dragstart', function(e) {
            _composerDragItem = this;
            this.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });
        card.addEventListener('dragend', function() {
            this.classList.remove('dragging');
            _composerDragItem = null;
            _composerUpdatePriorities(list);
        });
        card.addEventListener('dragover', function(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            if (!_composerDragItem || _composerDragItem === this) return;
            var rect = this.getBoundingClientRect();
            var mid = rect.top + rect.height / 2;
            if (e.clientY < mid) {
                list.insertBefore(_composerDragItem, this);
            } else {
                list.insertBefore(_composerDragItem, this.nextSibling);
            }
        });
    });
}

function _composerUpdatePriorities(list) {
    var cards = list.querySelectorAll('.composer-card');
    var recipeBlocks = _composerGetRecipeBlocks();
    var priority = 1;
    cards.forEach(function(card) {
        var id = card.getAttribute('data-block-id');
        var rb = recipeBlocks.find(function(b) { return b.id === id; });
        if (rb) {
            rb.priority = priority++;
            _composerDirty = true;
        }
    });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _composerGetRecipeBlocks() {
    if (_composerActiveRecipe && _composerActiveRecipe.blocks) {
        return _composerActiveRecipe.blocks;
    }
    // Build from available blocks with defaults
    return _composerBlocks.map(function(b, i) {
        return {
            id: b.id,
            enabled: b.default_enabled,
            priority: i + 1,
            config: {},
        };
    });
}

function getComposerRecipeId() {
    return _composerRecipeId;
}

function getComposerThesis() {
    return _composerThesis;
}

function _getCSRF() {
    return (typeof window !== 'undefined' && window.getCSRFToken) ? window.getCSRFToken() : '';
}

function _posthogTrack(event, props) {
    if (typeof posthog !== 'undefined' && posthog.capture) {
        posthog.capture(event, props);
    }
}
