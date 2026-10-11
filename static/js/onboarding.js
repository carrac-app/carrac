/**
 * Onboarding utilities — chip toggles reused by contextual-nudges.js.
 * The onboarding modal has been removed in favor of the chart placeholder
 * getting-started guide and contextual nudges for profile setup.
 */

// ── Reusable chip utilities ──
function onbChip(value, label) {
    return '<label style="display:inline-flex;align-items:center;cursor:pointer;">' +
        '<input type="checkbox" value="' + value + '" style="display:none;">' +
        '<span style="font-size:12px;padding:4px 10px;border-radius:14px;background:#0d1117;color:#8b949e;' +
        'border:1px solid #30363d;transition:all 0.15s;">' + label + '</span></label>';
}

function setupChipToggles(container) {
    if (container.dataset.chipTogglesBound) return;
    container.dataset.chipTogglesBound = '1';
    container.querySelectorAll('label').forEach(function(label) {
        label.addEventListener('click', function(e) {
            var cb = this.querySelector('input');
            if (!cb) return;
            if (e.target !== cb) { e.preventDefault(); cb.checked = !cb.checked; }
            var span = this.querySelector('span');
            if (cb.checked) {
                span.style.background = 'rgba(63,185,80,0.1)';
                span.style.color = '#3fb950';
                span.style.borderColor = '#3fb950';
            } else {
                span.style.background = '#0d1117';
                span.style.color = '#8b949e';
                span.style.borderColor = '#30363d';
            }
        });
    });
}

// No-op — kept so the call in main.js stays valid
function initOnboarding() {}
