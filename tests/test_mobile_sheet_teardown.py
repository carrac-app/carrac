"""Mobile ☰ hamburger must stay tappable: static guard tests.

Failure mode: the PWA v2 mobile chart's "feature sheet" system (⋮ More / TF /
Indicator sheets — .mobile-sheet-backdrop + .mobile-feature-sheet), if
appended straight to <body>, sits as a SIBLING of the advanced-chart-modal
(#klineModal / .advanced-chart-modal), not as its descendant. Because of
that, hiding the chart modal did not automatically remove them, and any path
that left the chart open→closed without every JS close-path running (the
concrete case: navigating away via the chart's "Journal" bottom-bar
button — a real cross-document nav — while a sheet was open, then returning
via the browser/OS back button, which restores the page from bfcache without
re-running any init/close JS) could strand a leftover backdrop/sheet at
z-index 25999-26001 over the markets-list header, silently eating every tap
there — including the ☰ menu (#more-menu-btn), which read as "untappable"
with no visible cause.

The design has three parts, each guarded below:
  1. openMobileSheet() nests the backdrop+sheet inside klineModal instead of
     <body>, so display:none on the modal kills the whole subtree (the
     structural/primary fix — no JS teardown call required for the common
     "closed via the in-app back arrow" case).
  2. Every close path (closeMobileSheet(), _destroyMobileSheet(), and the
     pageshow(persisted) bfcache-restore hook — the one case where NO
     close-path JS runs at all) funnels through a single shared
     closeAllMobileSheets() helper, so there's one teardown to keep correct
     instead of several drifting copies (belt/suspenders for the residual
     cases the structural approach doesn't cover, e.g. klineModal not existing yet
     when a sheet is opened).
  3. #more-menu-btn / .more-menu-wrapper get an explicit mobile z-index above
     the whole 25000-28000 chart-modal overlay family, as a defensive
     backstop independent of whether the teardown above is ever bypassed by
     some future code path.

These are static/grep-style checks (this repo's test suite runs against
SQLite while prod is Postgres and has no headless-browser harness for CSS/DOM
behavior — see test_report_editorial_css_clean.py for the established
pattern) — they assert the *shape* of the design is present and wired together
correctly, not runtime behavior.
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


def test_close_all_mobile_sheets_helper_exists():
    """The shared teardown helper must exist as a real function definition."""
    js = _chart_app_js()
    assert re.search(r'function\s+closeAllMobileSheets\s*\(\s*\)\s*\{', js), (
        "closeAllMobileSheets() is missing — the hamburger fix relies on a "
        "single shared teardown for the mobile feature-sheet backdrop/sheet."
    )


def test_close_all_mobile_sheets_removes_both_leak_classes():
    """The helper must actually remove both leak-prone elements by class,
    not just null out the module-scoped variables — query-based cleanup is
    what makes it safe to call after a bfcache restore where JS state and
    DOM could have drifted apart."""
    js = _chart_app_js()
    m = re.search(r'function\s+closeAllMobileSheets\s*\(\s*\)\s*\{(.*?)\n\}', js, re.DOTALL)
    assert m, "could not locate closeAllMobileSheets() body"
    body = m.group(1)
    assert "querySelectorAll('.mobile-sheet-backdrop')" in body, (
        "closeAllMobileSheets() must query-remove .mobile-sheet-backdrop elements"
    )
    assert "querySelectorAll('.mobile-feature-sheet')" in body, (
        "closeAllMobileSheets() must query-remove .mobile-feature-sheet elements"
    )


def test_close_mobile_sheet_delegates_to_shared_helper():
    """closeMobileSheet() (called from ~10 in-app sites, including
    closeAdvancedChartModal) must route through the shared helper rather than
    duplicating its own teardown logic — otherwise the two can drift apart."""
    js = _chart_app_js()
    m = re.search(r'function\s+closeMobileSheet\s*\(\s*\)\s*\{(.*?)\n\}', js, re.DOTALL)
    assert m, "could not locate closeMobileSheet() body"
    assert "closeAllMobileSheets()" in m.group(1), (
        "closeMobileSheet() must call closeAllMobileSheets() — found a body "
        "that doesn't reference the shared helper, i.e. teardown logic has "
        "drifted apart again."
    )


def test_destroy_mobile_sheet_delegates_to_shared_helper():
    """_destroyMobileSheet() — the legacy internal name still called from
    openMobileSheet()'s own 'replace existing sheet' step — must also
    forward to the shared helper, not keep its own removal logic that only
    trusts the (possibly stale) module vars."""
    js = _chart_app_js()
    m = re.search(r'function\s+_destroyMobileSheet\s*\(\s*\)\s*\{(.*?)\n\}', js, re.DOTALL)
    assert m, "could not locate _destroyMobileSheet() body"
    assert "closeAllMobileSheets()" in m.group(1), (
        "_destroyMobileSheet() must forward to closeAllMobileSheets(), not "
        "reimplement its own _mobileBackdropEl/_mobileSheetEl-only removal."
    )


def test_open_mobile_sheet_nests_in_kline_modal_not_body():
    """Primary structural fix: the backdrop + sheet must be appended into
    klineModal (so they die with it via display:none), not document.body
    directly. This is the actual leak fix; the shared-helper teardown above
    is the backstop for the cases this doesn't structurally cover."""
    js = _chart_app_js()
    m = re.search(r'function\s+openMobileSheet\s*\(tab\)\s*\{(.*?)\nfunction\s+closeMobileSheet',
                  js, re.DOTALL)
    assert m, "could not locate openMobileSheet() body"
    body = m.group(1)

    # Pins: no direct-to-body appends.
    assert "document.body.appendChild(_mobileBackdropEl)" not in body, (
        "openMobileSheet() still appends the sheet backdrop straight to "
        "<body> — a body-level backdrop "
        "survives the chart modal being hidden."
    )
    assert "document.body.appendChild(_mobileSheetEl)" not in body, (
        "openMobileSheet() still appends the feature sheet straight to "
        "<body> — a body-level sheet "
        "survives the chart modal being hidden."
    )

    # Positive guard: they must be appended into a klineModal-derived host.
    assert re.search(r'klineModal', body), (
        "openMobileSheet() must reference klineModal — "
        "expected it to host the backdrop/sheet so they die with the modal."
    )
    assert ".appendChild(_mobileBackdropEl)" in body, "backdrop is never appended anywhere"
    assert ".appendChild(_mobileSheetEl)" in body, "feature sheet is never appended anywhere"


def test_open_mobile_sheet_unconditionally_clears_before_opening():
    """The 'close existing sheet first' step must be unconditional
    (closeAllMobileSheets(), not `if (_mobileSheetEl) ...`) so a DOM/state
    mismatch (e.g. after a bfcache restore) can't result in a second sheet
    stacking on top of a stale one instead of replacing it."""
    js = _chart_app_js()
    m = re.search(r'function\s+openMobileSheet\s*\(tab\)\s*\{(.*?)\n\n\s*_mobileActiveTab = tab;',
                  js, re.DOTALL)
    assert m, "could not locate openMobileSheet()'s pre-amble"
    preamble = m.group(1)
    assert "closeAllMobileSheets();" in preamble, (
        "openMobileSheet() must unconditionally call closeAllMobileSheets() "
        "before creating a new sheet"
    )
    assert "if (_mobileSheetEl) _destroyMobileSheet();" not in preamble, (
        "openMobileSheet() still gates its pre-cleanup on the _mobileSheetEl "
        "module var — this can skip cleanup when DOM/state has drifted apart "
        "(e.g. after a bfcache restore)."
    )


def test_pageshow_persisted_hook_sweeps_mobile_sheets():
    """The bfcache-restore hook (pageshow, event.persisted) is the ONE path
    where no application JS runs at all before the page is shown again — it
    must call closeAllMobileSheets() as a catch-all, per MDN/web.dev: bfcache
    restores the full prior DOM + JS heap verbatim with no init/close code
    re-executing."""
    js = _chart_app_js()
    m = re.search(r"window\.addEventListener\('pageshow',\s*function\s*\(e\)\s*\{(.*?)\n    \}\);",
                  js, re.DOTALL)
    assert m, "could not locate the window 'pageshow' listener"
    body = m.group(1)
    assert "e.persisted" in body, "pageshow handler must gate on event.persisted"
    assert "closeAllMobileSheets()" in body, (
        "the pageshow(persisted) bfcache-restore hook must call "
        "closeAllMobileSheets() — this is the one code path where a leftover "
        "sheet/backdrop can be restored with NO close-path JS having run."
    )


def test_hamburger_has_defensive_mobile_zindex_above_chart_overlay_family():
    """Belt for the teardown fix's suspenders: #more-menu-btn's wrapper must
    carry an explicit z-index, scoped to mobile, high enough to sit above the
    chart-modal's entire 25000-28000 overlay family — a backstop in case
    something the teardown fix doesn't anticipate leaves a stray overlay over
    the header."""
    css = _main_css_text()
    m = re.search(
        r'@media\s*\(max-width:\s*768px\)\s*\{\s*\.more-menu-wrapper\s*\{\s*z-index:\s*(\d+);',
        css,
    )
    assert m, (
        ".more-menu-wrapper has no mobile-scoped z-index rule — "
        "the hamburger has no defensive stacking backstop."
    )
    z = int(m.group(1))
    assert z >= 28001, (
        f".more-menu-wrapper z-index ({z}) does not clear the highest known "
        "chart-modal overlay z-index found in this codebase (28000 — "
        ".news-preview-overlay / .toolbar-tooltip)."
    )


def test_scroll_fix_and_search_pill_css_blocks_untouched_by_this_file():
    """Isolation guard: this file's CSS must not touch the
    sibling-owned 'Mobile markets-list scroll fix' block or the header
    search-bar-pill block, to keep the two sub-fixes conflict-free
    to merge."""
    css = _main_css_text()
    assert "Mobile markets-list scroll fix:" in css, (
        "the scroll-fix comment marker is gone — did an unrelated edit touch "
        "that block?"
    )
    # The hamburger z-index rule must live in its own block, not be folded
    # into either of the other two known mobile blocks.
    scroll_fix_idx = css.index("Mobile markets-list scroll fix:")
    search_pill_idx = css.index("Header search BAR:")
    hamburger_idx = css.index("z-index: 28100")
    assert not (scroll_fix_idx < hamburger_idx < scroll_fix_idx + 1200), (
        "the hamburger z-index rule appears to have been folded into the "
        "scroll-fix block — keep it isolated."
    )
    assert not (search_pill_idx < hamburger_idx < search_pill_idx + 2000), (
        "the hamburger z-index rule appears to have been folded into the "
        "search-bar-pill block — keep it isolated."
    )
