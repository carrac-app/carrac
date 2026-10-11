"""The chart app's own classic scripts, in load order (main.js last).

Python twin of tests/js/chart-app-sources.cjs. Tests that check "the chart
app" as a whole read the concatenation, so a function moving between these
files does not silently narrow what a test can see.
tests/test_chart_standalone_scripts.py keeps this list, the JS twin and both
templates in step.
"""
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent.parent / 'static' / 'js'

CHART_APP_FILES = (
    'app-core.js',
    'indicator-prefs.js',
    'chart-cache.js',
    'chart-modal.js',
    'chart-events.js',
    'chart-keyboard.js',
    'position-tools.js',
    'chart-indicators.js',
    'drawing-editing.js',
    'draw-tool-palette.js',
    'chart-loading.js',
    'chart-render.js',
    'mobile-chart.js',
    'stale-tab-recovery.js',
    'main.js',
)


def chart_app_source():
    return '\n'.join((JS_DIR / f).read_text(encoding='utf-8') for f in CHART_APP_FILES)
