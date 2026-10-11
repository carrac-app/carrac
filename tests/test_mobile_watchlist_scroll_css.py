"""Pins the mobile/PWA markets-list scroll.

Three independent mechanisms can each kill list scrolling on mobile:
  - The desktop split-layout panel chain
    (.split-layout / .right-panel / .panel-content-area) uses
    overflow:hidden + flex/grid heights; without a mobile override those
    ancestors clip the list and kill page scroll.
  - .container.split-active is position:fixed (the desktop full-viewport
    anchor). It can leak onto a mobile viewport (briefly-wrong innerWidth at
    first paint) and, if never cleaned up, also kills scroll.
  - The grouped-watchlist render puts draggable="true" on every watchlist
    <tr> for desktop drag-reorder, with no viewport gate. Chrome for Android
    has supported touch-initiated native HTML5 drag since Chrome Android 18,
    so a vertical swipe starting on a row can be captured as a native drag
    instead of a scroll.

Each fix lives entirely in static/css/main.css (+ the split-active JS guard
in main.js for the second) and nothing here needs a browser/DOM — these
are plain string/regex assertions against the shipped CSS/JS, matching the
existing tests/test_report_editorial_css_clean.py pattern. They exist to
catch a future refactor that silently drops one of the three fixes, not to
re-derive the fix itself.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parents[1]
CSS_PATH = REPO_ROOT / "static" / "css" / "main.css"
MAIN_JS_PATH = REPO_ROOT / "static" / "js" / "main.js"


def _strip_all_media_blocks(css: str) -> str:
    """Return css with every `@media {...}` rule (any condition) removed
    entirely, leaving only top-level (unconditional) rules."""
    out = []
    i = 0
    n = len(css)
    while i < n:
        m = re.compile(r'@media[^{]*\{').match(css, i)
        if m:
            depth = 1
            j = m.end()
            while j < n and depth:
                if css[j] == '{':
                    depth += 1
                elif css[j] == '}':
                    depth -= 1
                j += 1
            i = j
            continue
        out.append(css[i])
        i += 1
    return "".join(out)


def _mobile_media_blocks(css: str):
    """Yield the body text of every top-level `@media (max-width: 768px)`-ish
    block (also matches `@media screen and (max-width: 768px)`), so we can
    assert on rules without caring which specific mobile block they live in.
    """
    blocks = []
    for m in re.finditer(r'@media[^{]*max-width:\s*768px[^{]*\{', css):
        start = m.end()
        depth = 1
        i = start
        while i < len(css) and depth:
            if css[i] == '{':
                depth += 1
            elif css[i] == '}':
                depth -= 1
            i += 1
        blocks.append(css[start:i - 1])
    return blocks


def test_panel_chain_overflow_visible_on_mobile():
    """The split-layout/right-panel/panel-content-area chain
    must still be forced to overflow:visible (+ panel-content-area height:auto)
    below the mobile breakpoint, or the ancestors clip the list again."""
    css = CSS_PATH.read_text()
    mobile_css = "\n".join(_mobile_media_blocks(css))

    for selector_fragment in (".split-layout", ".right-panel", ".panel-content-area"):
        assert selector_fragment in mobile_css, (
            f"{selector_fragment} is no longer overridden in a max-width:768px "
            "block in main.css — the desktop split-layout overflow chain "
            "would clip the mobile list."
        )
    assert re.search(r'\.panel-content-area\s*\{[^}]*height:\s*auto', mobile_css), (
        ".panel-content-area { height: auto } is missing from the mobile "
        "media query — without it the base `height: 100%` grid-row rule can "
        "re-clip the list on mobile."
    )
    assert re.search(r'\.table-wrapper\s*\{[^}]*overflow-y:\s*visible', mobile_css), (
        ".table-wrapper { overflow-y: visible } is missing from the mobile "
        "media query."
    )


def test_split_active_cannot_lock_body_scroll_on_mobile():
    """body.split-active still sets overflow:hidden
    unconditionally (it's the desktop full-viewport anchor), so the JS
    guard that force-drops the class on any mobile viewport must still
    exist and still be unconditional (load/resize/orientationchange)."""
    css = CSS_PATH.read_text()
    assert re.search(r'body\.split-active\s*\{[^}]*overflow:\s*hidden', css), (
        "body.split-active must set overflow:hidden (the desktop "
        "full-viewport anchor); the JS guard below depends on it."
    )

    js = MAIN_JS_PATH.read_text()
    assert "_dropStaleSplitActive" in js, (
        "_dropStaleSplitActive must exist in main.js: it force-drops a stuck "
        "`split-active` class (which makes .container position:fixed + kills "
        "body scroll) whenever window.innerWidth <= 768. Without it, "
        "split-active can leak onto a mobile viewport and freeze the list."
    )
    for hook in ("addEventListener('load'", "addEventListener('resize'",
                 "addEventListener('orientationchange'"):
        assert hook in js, (
            f"main.js's split-active guard must wire {hook} — all three "
            "lifecycle hooks are needed to catch a viewport "
            "that briefly misreports >768px at first paint."
        )


def test_watchlist_row_drag_neutralised_on_mobile():
    """draggable="true" watchlist rows must have
    native drag neutralised below the mobile breakpoint, or a touch-drag
    starting on a row can be captured as native HTML5 drag instead of a
    page scroll (Chrome for Android has supported touch-initiated drag
    since Chrome Android 18)."""
    css = CSS_PATH.read_text()
    mobile_css = "\n".join(_mobile_media_blocks(css))

    draggable_rule = re.search(
        r'\.right-panel\s+\[draggable="true"\]\s*\{([^}]*)\}', mobile_css)
    assert draggable_rule, (
        ".right-panel [draggable=\"true\"] has no mobile override — the "
        "native touch-drag on watchlist rows would block page scroll."
    )
    body = draggable_rule.group(1)
    assert "-webkit-user-drag" in body and "none" in body, (
        "the mobile draggable-row override lost -webkit-user-drag:none "
        "."
    )
    assert re.search(r'touch-action:\s*pan-y', body), (
        "the mobile draggable-row override lost touch-action:pan-y — this "
        "is the standards-based backstop that tells the browser a vertical "
        "touch-drag on a row is page panning, not a native-drag candidate "
        "."
    )

    # The neutralisation must not follow the row into desktop width — manual
    # drag-reorder is a desktop-only affordance and should stay live there.
    # Strip every @media block (any condition) and confirm the rule only
    # exists inside one of them, never unconditionally at the top level.
    unconditional_css = _strip_all_media_blocks(css)
    assert not re.search(r'\.right-panel\s+\[draggable="true"\]\s*\{[^}]*-webkit-user-drag:\s*none',
                          unconditional_css), (
        "the drag-neutralising rule is applied unconditionally (outside any "
        "@media block) — this would also disable desktop drag-reorder."
    )


def test_watchlist_rows_still_draggable_in_markup():
    """Sanity check: the draggable-rows fix is CSS-only. main.js must still emit
    draggable="true" on watchlist rows (desktop reorder is unaffected) —
    if this ever disappears, the CSS guard above is dead weight and the
    JS-side manual-reorder feature has separately broken."""
    # CreateRow moved to screener-table.js with the table engine.
    js = (REPO_ROOT / "static" / "js" / "screener-table.js").read_text()
    assert 'draggable="true"' in js, (
        "screener-table.js must emit draggable=\"true\" on watchlist rows — "
        "either the manual-reorder feature was removed (update/remove this "
        "guard) or something upstream broke it."
    )
