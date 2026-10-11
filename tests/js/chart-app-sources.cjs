// The chart app's own classic scripts, in the order templates/index.html and
// templates/chart_standalone.html load them (main.js last). Tests that check
// "the chart app" as a whole read their concatenation, so a function moving
// between these files does not silently narrow what a test can see.
// tests/test_chart_standalone_scripts.py keeps this list and both templates
// in step.
const fs = require('fs');
const path = require('path');

const CHART_APP_FILES = [
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
];

const JS_DIR = path.join(__dirname, '..', '..', 'static', 'js');

function chartAppSource() {
    return CHART_APP_FILES
        .map((f) => fs.readFileSync(path.join(JS_DIR, f), 'utf8'))
        .join('\n');
}

module.exports = { CHART_APP_FILES, chartAppSource };
