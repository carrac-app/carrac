"""Ensure report-editorial.css contains no global / bare-element selectors.

The stylesheet is loaded into the main trading app (templates/index.html) for
the side panel.  Any global rule would apply app-wide — only :root{}, @-rules,
and class/id-scoped selectors are permitted in the shared module.

Collision guard:
All non-:root selectors must be namespaced under .ed-scope so the editorial
design system cannot override live trading-app components such as:
  - .when  (radar.js freshness pills)
  - .badge (intelligence.js score badges)
  - .lvl   (details-drawer.js / scenarios.js key-level rows)
  - .btn   (main.css button base style)
…and the 50+ other class names shared between the editorial CSS and the
main-app JS that renders into index.html.

The fix: every selector in report-editorial.css (except :root and @media
headers) must include .ed-scope as an ancestor.  The editorial page body and
the panel container in index.html carry class="ed-scope" so all editorial
elements are captured, while main-app elements outside those roots are not.
"""
import pathlib
import re

CSS_PATH = pathlib.Path(__file__).parents[1] / "static" / "css" / "report-editorial.css"

# Main-app files whose class usage must not be shadowed by report-editorial.css
_APP_FILES = [
    pathlib.Path(__file__).parents[1] / "static" / "css" / "main.css",
    pathlib.Path(__file__).parents[1] / "static" / "css" / "carrac-overhaul.css",
    pathlib.Path(__file__).parents[1] / "templates" / "index.html",
    pathlib.Path(__file__).parents[1] / "static" / "js" / "radar.js",
    pathlib.Path(__file__).parents[1] / "static" / "js" / "intelligence.js",
    pathlib.Path(__file__).parents[1] / "static" / "js" / "details-drawer.js",
    pathlib.Path(__file__).parents[1] / "static" / "js" / "scenarios.js",
]


def test_no_universal_selector():
    """*{...} must not appear in the shared module."""
    css = CSS_PATH.read_text()
    assert "*{" not in css and "* {" not in css, (
        "report-editorial.css contains a universal '*{' rule — "
        "relocate it to page <style> blocks and #panel-report-view in main.css"
    )


def test_no_bare_body_html():
    """Bare body{} / html{} selectors must not appear in the shared module."""
    css = CSS_PATH.read_text()
    # negative lookbehind for . and # so .body{} / #body{} (class/id) are allowed
    for pattern in (r'(?<![.#\w])body\s*\{', r'(?<![.#\w])html\s*\{'):
        assert not re.search(pattern, css), (
            f"report-editorial.css contains a bare '{pattern}' rule — "
            "relocate it to the page-level <style> blocks"
        )


def test_all_selectors_scoped_under_ed_scope():
    """Every non-:root selector in report-editorial.css must contain .ed-scope.

    This is the collision guard: by requiring the .ed-scope ancestor on
    every rule, no editorial CSS can bleed onto main-app components that happen
    to share a class name (confirmed: .when, .badge, .lvl, .btn; 50+ in total).

    Failure means someone added a new unscoped rule — add .ed-scope to it or
    update the editorial page <body> / panel container accordingly.
    """
    css = CSS_PATH.read_text()

    # Strip comments so they don't confuse selector extraction
    css_no_comments = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)

    # Collect every selector group (text before each '{').
    # Walk the CSS tracking brace depth so nested rules inside @media are also checked.
    violations = []
    depth = 0
    selector_buf = []
    i = 0
    n = len(css_no_comments)

    while i < n:
        ch = css_no_comments[i]
        if ch == '{':
            selector_text = ''.join(selector_buf).strip()
            selector_buf = []
            depth += 1
            if selector_text:
                # Skip :root and @-rule headers
                if not selector_text.startswith(':root') and not selector_text.startswith('@'):
                    # Each comma-separated part must include .ed-scope
                    for part in selector_text.split(','):
                        part = part.strip()
                        if part and '.ed-scope' not in part:
                            violations.append(part)
        elif ch == '}':
            depth -= 1
            selector_buf = []
        else:
            selector_buf.append(ch)
        i += 1

    assert not violations, (
        "report-editorial.css has unscoped selectors that leak into the main app.\n"
        "Every non-:root selector must include .ed-scope.\n"
        "Add .ed-scope to the selector, or scope via the page <body class='ed-scope'>\n"
        "and the panel <div class='... ed-scope'>.\n\n"
        "Flagged selectors:\n" + "\n".join(f"  {s}" for s in sorted(set(violations)))
    )
