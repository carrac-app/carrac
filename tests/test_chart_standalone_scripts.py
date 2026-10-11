"""chart_standalone.html must load every script the chart app calls unguarded
on the chart path — the page has no bundler, so a missing <script> is a runtime
ReferenceError that surfaces as "Chart failed to load". A missing indicator-registry.js, freehand.js or
confluence-zones.js is exactly this shape.

The set is DERIVED, not hand-listed: every bare statement call in the chart
app's own files (tests/chart_app_sources.py — `name(...);` at the start of a
line, no `typeof name` / `window.name` guard within the two lines above) whose
definition lives in another static/js file. Call sites inside main.js's
window.onload are skipped — the standalone page sets window.isStandaloneChart
and that whole block is bypassed there.

Both templates must also load the chart app's files in the same order: they
are classic scripts sharing one global scope, so the order is part of the
contract.
"""
import re
from pathlib import Path

from tests.chart_app_sources import CHART_APP_FILES, chart_app_source


_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / 'static' / 'js'
_MAIN = chart_app_source()
_STANDALONE = (_ROOT / 'templates' / 'chart_standalone.html').read_text(encoding='utf-8')
_INDEX = (_ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')

# Names the chart app may call bare even though their file is not loaded on the
# standalone page: each sits behind DOM the page does not render, so the call
# can never execute there. NAME-level on purpose — a file-level allowlist would hide
# bare calls such as updateDrawerAll()/loadPlaysForChart() that ARE on the
# chart path. Each entry needs a reason.
_ALLOWED_UNGUARDED = {
    '_handlePlayPathClick': 'click handler on a play-path overlay inside the main-page plays panel',
    'openDrawer': 'keyboard tab-cycling of the main-page asset drawer',
    'toggleDrawer': 'click handler attached to .drawer-tab, absent on the standalone page',
    'toggleDrawerFullscreen': 'click handler attached to the drawer fullscreen button, absent on the standalone page',
}


def _onload_span(src):
    start = src.find('window.onload = function')
    if start == -1:
        return (0, 0)
    i = src.index('{', start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return (start, j)
    return (start, len(src))


def _bare_calls(src):
    """name -> True if at least one call site is unguarded and outside onload."""
    lo, hi = _onload_span(src)
    lines = src.split('\n')
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    found = {}
    pat = re.compile(r"^\s*(?:if \([^)]*\)\s*)?([A-Za-z_]\w*)\([^;]*\);\s*(?://.*)?$")
    for idx, ln in enumerate(lines):
        m = pat.match(ln)
        if not m:
            continue
        name = m.group(1)
        if lo <= offsets[idx] <= hi:
            continue
        window = '\n'.join(lines[max(0, idx - 2):idx + 1])
        if f'typeof {name}' in window or f'window.{name}' in window:
            continue
        found[name] = True
    return found


def _definitions():
    defs = {}
    for p in _JS.glob('*.js'):
        if p.name in CHART_APP_FILES or p.name.endswith('.min.js'):
            continue
        for name in re.findall(r"^\s*function\s+(\w+)\s*\(", p.read_text(encoding='utf-8'), re.M):
            defs.setdefault(name, set()).add(p.name)
    return defs


def _required_files():
    own = set(re.findall(r"^\s*function\s+(\w+)\s*\(", _MAIN, re.M))
    defs = _definitions()
    need = {}
    for name in _bare_calls(_MAIN):
        if name in own or name not in defs:
            continue
        for f in defs[name]:
            need.setdefault(f, set()).add(name)
    return need


def test_analysis_still_sees_the_known_bootstraps():
    need = _required_files()
    assert 'freehandEnsureIndicator' in need.get('freehand.js', set())
    assert 'confluenceOnDataChange' in need.get('confluence-zones.js', set())
    assert 'liqHeatmapOnDataChange' in need.get('liq-heatmap.js', set())


def test_allowlist_entries_carry_a_reason():
    assert all(_ALLOWED_UNGUARDED.values())


def test_standalone_page_loads_every_file_the_chart_app_calls_bare():
    """Every unguarded bare call in the chart app must resolve on the
    standalone page: defined in the chart app, in a file the page loads, or
    explicitly allowed."""
    defs = _definitions()
    own = set(re.findall(r"^\s*function\s+(\w+)\s*\(", _MAIN, re.M))
    unresolved = {}
    for name in _bare_calls(_MAIN):
        if name in own or name not in defs or name in _ALLOWED_UNGUARDED:
            continue
        if any(f'js/{f}' in _STANDALONE for f in defs[name]):
            continue
        unresolved[name] = sorted(defs[name])
    assert not unresolved, (
        'the chart app calls these bare and chart_standalone.html loads none of their files '
        f'(load the file, or typeof-guard the call): {unresolved}')


def test_allowlist_is_not_stale():
    bare = _bare_calls(_MAIN)
    defs = _definitions()
    stale = [n for n in _ALLOWED_UNGUARDED
             if n not in bare or any(f'js/{f}' in _STANDALONE for f in defs.get(n, ()))]
    assert not stale, f'remove from _ALLOWED_UNGUARDED: {stale}'


def test_standalone_loads_freehand_before_the_chart_app():
    assert _STANDALONE.index('js/freehand.js') < _STANDALONE.index('js/' + CHART_APP_FILES[0])


def _chart_app_load_order(template):
    srcs = re.findall(r"<script[^>]*\bsrc=[^>]*?js/([\w.-]+\.js)", template)
    return [s for s in srcs if s in CHART_APP_FILES]


def test_both_templates_load_the_chart_app_files_in_order():
    """Each file appears exactly once, in CHART_APP_FILES order, on both
    pages. A file loaded early or twice can read a global before the file
    that declares it has run."""
    assert _chart_app_load_order(_INDEX) == list(CHART_APP_FILES)
    assert _chart_app_load_order(_STANDALONE) == list(CHART_APP_FILES)


def test_python_and_js_file_lists_agree():
    js_list = (_ROOT / 'tests' / 'js' / 'chart-app-sources.cjs').read_text(encoding='utf-8')
    block = js_list[js_list.index('CHART_APP_FILES = ['):js_list.index('];')]
    assert re.findall(r"'([\w.-]+\.js)'", block) == list(CHART_APP_FILES)
