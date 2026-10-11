"""HTML escaping lives in static/js/shared-utils.js; pages must load it first."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_HELPER = re.compile(
    r"function\s+(?!escapeHtml\b)(?:_?\w{0,3}[Ee]sc|_?escape|_?escHtml|_?escapeHTML)\w*\s*\("
    r"|\b_esc\("
)

# Scripts that call escapeHtml without defining it, and the pages that load them.
CONSUMERS = {
    "news-panel.js": "index.html",
    "plays-creation-form.js": "index.html",
    "plays-backtest-panel.js": "index.html",
    "journal-playbook-analytics.js": "journal.html",
}


def test_no_script_defines_its_own_html_escaper():
    offenders = []
    for path in (ROOT / "static" / "js").glob("*.js"):
        if path.name in {"shared-utils.js", "lucide-icons.js"} or path.name.endswith(".min.js"):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if LOCAL_HELPER.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()[:80]}")
    assert not offenders, offenders


def test_pages_load_shared_utils_before_their_escapers():
    for script, page in CONSUMERS.items():
        html = (ROOT / "templates" / page).read_text()
        shared = html.index("js/shared-utils.js")
        consumer = html.index(f"js/{script}")
        assert shared < consumer, f"{page}: shared-utils.js must precede {script}"
