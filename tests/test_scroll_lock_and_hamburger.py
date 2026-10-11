"""Mobile asset-selector list must scroll vertically and the ☰ hamburger
must stay tappable. Static guard tests.

Scroll half — root cause: openAdvancedChartModal /
closeAdvancedChartModal / closeChartModal wrote
`document.body.style.overflow = 'hidden'` / `''` as an IMPERATIVE inline
style. Clearing depended entirely on one of the two closers actually
running. Any modal exit that skips both — the concrete case: bfcache
restoring the page after chart -> Journal -> OS-back runs NO app JS at all
(pageshow(persisted), no init/close code re-executes) — strands the inline
lock forever: page vertical scroll dead, while the list's own horizontal
overflow (table-wrapper's overflow-x, a separate scroll container
unaffected by body) still works. That is exactly the "scrolls right, not
down" symptom. test_mobile_watchlist_scroll_css.py covers three ways the
LIST's own scrolling can die; this is one level up, at the page/body level.

Fix: the lock is STATE-DERIVED — a `body.chart-modal-open` class + a CSS
rule — via a single `syncChartModalScrollLock()` function (chart-modal.js) that
is the one source of truth for that class, called from every
open/close/restore/startup path instead of trusting an imperative
write/clear pair to stay in sync.

Hamburger half — audited, NOT separately fixed. The ancestor chain of
#more-menu-btn (-> .more-menu-wrapper -> .action-bar-right -> .action-bar
(#sentiment-bar) -> .container -> main#main-content -> body) was walked
property-by-property for anything that forms a CSS stacking context
(position+z-index, transform, filter, opacity<1, will-change, contain,
isolation, backdrop-filter, mix-blend-mode, clip-path/mask): none of them
carry one in the STEADY mobile state (the one documented exception,
`.container.split-active` using position:fixed, is exactly the case that
_dropStaleSplitActive() forces off at mobile widths — see
test_split_active_cannot_lock_body_scroll_on_mobile). So the
z-index:28100 on the hamburger is NOT trapped inside an ancestor stacking
context and does not need to be raised or portalled. `.action-bar` is also
not position:sticky/fixed on its own — it scrolls with the page like any
other element. That is the simpler explanation: with
body's vertical scroll stuck 'hidden' (the SAME strand as the scroll half)
and the viewport scrolled even slightly away from the top when the lock
struck, the non-sticky header — hamburger included — slides off-screen and
STAYS unreachable, because the page can no longer scroll back up to it. No
covering overlay, no pointer-events block, no missing delegated-handler
case (templates/index.html's `data-action="toggleMoreMenu"` handler is
unconditional) is required to produce "untappable" — being scrolled out of
reach and stuck there looks identical. Fixing the scroll half therefore also
fixes the hamburger complaint, as a direct consequence rather than a
coincidence: the guards below make sure a future change can't silently
reopen either the imperative inline-overflow path or the stacking-context
trap that would make the z-index fix inert.

These are static/grep-style checks (this repo's test suite runs against
SQLite while prod is Postgres and has no headless-browser harness for
CSS/DOM behavior — see test_report_editorial_css_clean.py /
test_mobile_sheet_teardown.py for the established pattern).
"""
import pathlib
import re

from tests.chart_app_sources import chart_app_source

REPO_ROOT = pathlib.Path(__file__).parents[1]
MAIN_CSS = REPO_ROOT / "static" / "css" / "main.css"


def _chart_app_js():
    return chart_app_source()


def _main_css_text():
    return MAIN_CSS.read_text()


def _function_body(js, name, params=r"\([^)]*\)"):
    """Extract a top-level `function name(...) { ... }` body via brace
    counting (regex alone can't balance nested braces)."""
    m = re.search(r'function\s+' + re.escape(name) + r'\s*' + params + r'\s*\{', js)
    assert m, "could not locate function %s()" % name
    start = m.end()
    depth = 1
    i = start
    while i < len(js) and depth:
        if js[i] == '{':
            depth += 1
        elif js[i] == '}':
            depth -= 1
        i += 1
    return js[start:i - 1]


def test_sync_chart_modal_scroll_lock_exists():
    """The single source-of-truth function must exist and accept the
    cleanLegacyInline opt-in flag (see module docstring for why that flag
    is opt-in rather than applied on every call)."""
    js = _chart_app_js()
    assert re.search(r'function\s+syncChartModalScrollLock\s*\(\s*cleanLegacyInline\s*\)\s*\{', js), (
        "syncChartModalScrollLock(cleanLegacyInline) is missing or its "
        "signature changed — this single function is the source of truth "
        "for the chart-modal body scroll lock."
    )


def test_sync_chart_modal_scroll_lock_toggles_class_from_state():
    """The function must derive the lock from the LIVE klineModal/chartModal
    'visible' state via classList.toggle, not from any imperative flag."""
    js = _chart_app_js()
    body = _function_body(js, 'syncChartModalScrollLock', params=r'\(\s*cleanLegacyInline\s*\)')
    assert "klineModal" in body and "chartModal" in body, (
        "syncChartModalScrollLock() must check both klineModal and the "
        "legacy chartModal's 'visible' state"
    )
    assert re.search(r"classList\.toggle\(\s*'chart-modal-open'", body), (
        "syncChartModalScrollLock() must toggle the 'chart-modal-open' "
        "class based on computed modal-open state"
    )


def test_legacy_inline_clear_is_gated_and_conditional():
    """The legacy-inline-clear belt must only run when cleanLegacyInline is
    truthy AND no modal is open — never unconditionally, or it risks
    clobbering an unrelated modal's (help/premium/api-status/etc.) own
    legitimate inline overflow lock (see module docstring)."""
    js = _chart_app_js()
    body = _function_body(js, 'syncChartModalScrollLock', params=r'\(\s*cleanLegacyInline\s*\)')
    assert re.search(r'if\s*\(\s*cleanLegacyInline\s*&&\s*!modalOpen', body), (
        "the legacy inline-overflow clear inside syncChartModalScrollLock() "
        "must be gated on `cleanLegacyInline && !modalOpen` — an "
        "unconditional clear would strip other modals' legitimate inline "
        "overflow locks (e.g. a bfcache-restored Premium modal)."
    )


def test_no_imperative_body_overflow_writes_outside_the_sync_function():
    """Pins that openAdvancedChartModal / closeAdvancedChartModal /
    closeChartModal must not write document.body.style.overflow directly
    anywhere in the chart app — the only ACTUAL CODE occurrence of that
    pattern across its files must be the legacy-clear belt inside
    syncChartModalScrollLock() itself. Comments (which may mention the
    old pattern in prose) are stripped first so they can't produce false positives — the load-bearing
    documentation shouldn't have to avoid describing what it fixed."""
    js = _chart_app_js()
    js_no_comments = re.sub(r'//[^\n]*', '', js)
    sync_body = _function_body(js_no_comments, 'syncChartModalScrollLock', params=r'\(\s*cleanLegacyInline\s*\)')
    js_without_sync_fn = js_no_comments.replace(sync_body, '')

    writes = re.findall(r"document\.body\.style\.overflow\s*=", js_without_sync_fn)
    assert not writes, (
        "found %d imperative document.body.style.overflow write(s) in "
        "the chart app outside syncChartModalScrollLock() — the lock must be "
        "derived from state via the 'chart-modal-open' class, not written "
        "inline at each call site." % len(writes)
    )


def test_close_chart_modal_uses_sync_function():
    js = _chart_app_js()
    body = _function_body(js, 'closeChartModal')
    assert "syncChartModalScrollLock()" in body, (
        "closeChartModal() must call syncChartModalScrollLock() instead of "
        "writing document.body.style.overflow directly"
    )
    assert "document.body.style.overflow" not in body


def test_open_advanced_chart_modal_impl_uses_sync_function():
    js = _chart_app_js()
    body = _function_body(js, '_openAdvancedChartModalImpl')
    assert "syncChartModalScrollLock()" in body, (
        "_openAdvancedChartModalImpl() must call syncChartModalScrollLock() "
        "instead of writing document.body.style.overflow directly"
    )
    assert "document.body.style.overflow" not in body


def test_close_advanced_chart_modal_uses_sync_function():
    js = _chart_app_js()
    body = _function_body(js, 'closeAdvancedChartModal')
    assert "syncChartModalScrollLock()" in body, (
        "closeAdvancedChartModal() must call syncChartModalScrollLock() "
        "instead of writing document.body.style.overflow directly"
    )
    assert "document.body.style.overflow" not in body


def test_pageshow_persisted_hook_syncs_scroll_lock():
    """The bfcache-restore hook is the one path where NO application JS
    runs at all before the page is shown again (same reasoning as
    closeAllMobileSheets()) — it must re-derive
    the scroll lock from the restored klineModal/chartModal state."""
    js = _chart_app_js()
    m = re.search(r"window\.addEventListener\('pageshow',\s*function\s*\(e\)\s*\{(.*?)\n    \}\);",
                  js, re.DOTALL)
    assert m, "could not locate the window 'pageshow' listener"
    body = m.group(1)
    assert re.search(r"e\.persisted\s*&&\s*typeof\s+syncChartModalScrollLock\s*===\s*'function'", body), (
        "the pageshow(persisted) hook must call syncChartModalScrollLock() "
        "— without it, a bfcache-restored page can keep a stranded scroll "
        "lock (or a stale one) forever, since no other code runs on this "
        "path."
    )
    # Must NOT pass cleanLegacyInline=true here — see module docstring: an
    # unrelated modal could legitimately be the thing open post-restore.
    assert "syncChartModalScrollLock(true)" not in body, (
        "the pageshow(persisted) hook must call syncChartModalScrollLock() "
        "with NO arguments (cleanLegacyInline must stay false here) — "
        "passing true risks clobbering a different modal's own legitimate "
        "inline overflow lock if IT is what's actually open post-restore."
    )


def test_window_onload_runs_migration_belt_before_auto_open():
    """Cold-startup migration belt: an already-stranded PWA (still holding
    an imperative inline lock left by an earlier session) must
    self-heal on next launch, before the auto-reopen-chart logic decides
    whether to lock the screen again for a real reason."""
    js = _chart_app_js()
    m = re.search(r'window\.onload\s*=\s*function\s*\(\)\s*\{(.*?)\n\};', js, re.DOTALL)
    assert m, "could not locate window.onload"
    onload_body = m.group(1)

    sync_idx = onload_body.find('syncChartModalScrollLock(true)')
    assert sync_idx != -1, (
        "window.onload must call syncChartModalScrollLock(true) — the only "
        "call site allowed to pass cleanLegacyInline=true (see module "
        "docstring for why it's safe only here: nothing has had a chance "
        "to legitimately open yet)."
    )

    auto_open_idx = onload_body.find('Auto-reopen chart from URL param')
    assert auto_open_idx != -1, "could not locate the auto-reopen-chart block in window.onload"
    assert sync_idx < auto_open_idx, (
        "syncChartModalScrollLock(true) must run BEFORE the auto-reopen-"
        "chart logic, so a stale strand is cleared first and doesn't race "
        "with (or get immediately re-set by) the real chart-modal open."
    )


def test_css_chart_modal_open_rule_locks_overflow():
    css = _main_css_text()
    assert re.search(r'body\.chart-modal-open\s*\{[^}]*overflow:\s*hidden', css), (
        "body.chart-modal-open { overflow: hidden } is missing from "
        "main.css — this is the CSS half of the state-derived lock; "
        "without it, toggling the class in JS does nothing."
    )


def test_css_chart_modal_open_rule_wins_over_split_active_in_cascade():
    """Two same-specificity `body.<class>` selectors resolve ties by source
    order. The chart-modal-open rule must be declared AFTER both
    `body.split-active` rules (the unconditional one and the mobile
    override) so that if the two classes were ever stuck on body
    simultaneously, the chart modal's lock — the one that actually
    reflects what's covering the screen — wins regardless of viewport."""
    css = _main_css_text()
    split_unconditional = css.index("body.split-active { padding: 0")
    split_mobile = css.index("body.split-active { padding: 8px; overflow: auto")
    chart_modal_open = css.index("body.chart-modal-open { overflow: hidden; }")
    assert split_unconditional < chart_modal_open, (
        "body.chart-modal-open must be declared after the unconditional "
        "body.split-active rule"
    )
    assert split_mobile < chart_modal_open, (
        "body.chart-modal-open must be declared after the mobile "
        "body.split-active override (@media max-width:768px), or a stuck "
        "split-active could still win the cascade on mobile"
    )


def test_bare_container_rule_has_no_stacking_context_property():
    """Stacking-context guard for the hamburger's z-index
    (test_hamburger_has_defensive_mobile_zindex_above_chart_
    overlay_family): that fix's own comment documents the one scenario
    that traps it — `.container` gaining position:fixed OUTSIDE the
    `.split-active` gate (already guarded at the JS layer by
    _dropStaleSplitActive(), tested in
    test_split_active_cannot_lock_body_scroll_on_mobile). This asserts the
    CSS-layer half of that invariant: the bare, unconditional `.container`
    rule must carry no position/transform/filter/opacity/will-change/
    contain/isolation — any of which would make `.container` a stacking
    context on its own and re-trap the hamburger's z-index:28100 inside it,
    silently making that fix inert again exactly as its own comment warns.
    """
    css = _main_css_text()
    m = re.search(r'^\.container\s*\{([^}]*)\}', css, re.MULTILINE)
    assert m, "could not locate the bare, unconditional .container rule"
    rule_body = m.group(1)
    stacking_props = ('position', 'transform', 'filter', 'opacity',
                       'will-change', 'contain', 'isolation',
                       'backdrop-filter', 'mix-blend-mode', 'clip-path',
                       'mask', 'perspective')
    for prop in stacking_props:
        assert not re.search(r'(^|[\s;{])' + prop + r'\s*:', rule_body), (
            "the bare .container rule now declares '%s' — this forms a "
            "CSS stacking context and traps any z-index set on a "
            "descendant (like .more-menu-wrapper's mobile z-index:28100) "
            "inside it, making the hamburger unreachable above body-level "
            "overlays again. If this is intentional, re-verify the "
            "hamburger fix (main.css '.more-menu-wrapper { z-index:' near "
            "'max-width: 768px') still clears whatever now creates this "
            "context." % prop
        )
