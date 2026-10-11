"""Every DOMPurify fallback must fail CLOSED.

the DOMPurify branch of `templates/morning_report.html` read:

    var html = typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(raw) : raw;
    body.innerHTML = html;

so when the DOMPurify CDN did not load, un-sanitized LLM-authored report
markdown went straight to innerHTML. Five sibling fallbacks in the same
codebase already escaped; this one was the exception, which is exactly the
shape that survives review — it LOOKS like the others.

This test is deliberately a SCAN rather than a check of that one line. Fixing
the single site does nothing to stop the next one being written the same way,
and the defect is defined by a pattern, not a location.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Files that render untrusted/LLM-authored content through DOMPurify.
SCANNED = [
    'templates/morning_report.html',
    'templates/morning_report_view.html',
    'static/js/ai-panel.js',
    'static/js/report-core.js',
]

# The fail-open shape: a ternary on DOMPurify's presence whose ELSE branch is a
# bare identifier — i.e. the raw value — rather than an escaping call.
_FAIL_OPEN_TERNARY = re.compile(
    r"typeof\s+DOMPurify\s*!==\s*['\"]undefined['\"]\s*\?"
    r"\s*DOMPurify\.sanitize\([^)]*\)\s*:\s*([A-Za-z_$][\w$]*)\s*[;,)]"
)

# Fallback expressions that ARE safe: they escape rather than pass through.
_SAFE_FALLBACK = re.compile(r'escapeHtml\s*\(|\.textContent\s*=')


def _sources():
    for rel in SCANNED:
        path = ROOT / rel
        if path.exists():
            yield rel, path.read_text(encoding='utf-8')


def test_no_dompurify_ternary_passes_raw_content_through():
    offenders = []
    for rel, text in _sources():
        for match in _FAIL_OPEN_TERNARY.finditer(text):
            fallback = match.group(1)
            # A ternary whose else-branch is a call to an escaper is fine; a
            # bare variable fails open.
            if not _SAFE_FALLBACK.search(match.group(0)):
                line = text[:match.start()].count('\n') + 1
                offenders.append(f"{rel}:{line} -> falls back to raw `{fallback}`")
    assert not offenders, (
        "DOMPurify fallback passes un-sanitized content through:\n  "
        + "\n  ".join(offenders)
    )


# Every DOMPurify guard site, with the fallback a human has reviewed. A NEW
# site fails this test until somebody classifies it — which is the point. The
# alternative, a regex that decides for itself whether a fallback is "safe",
# was tried first and immediately mis-flagged the two that ARE safe
# (ai-panel.js strips all tags; report-core.js re-escapes the four structural
# tags it had intentionally reintroduced). A guard that cannot see what it is
# guarding is decoration, so this one does not pretend to judge — it pins.
REVIEWED_FALLBACKS = {
    'templates/morning_report.html': 3,      # :1162 textContent-escape,
                                             # :2348 textContent-escape, :3608 escapeHtml
    'templates/morning_report_view.html': 1,  # :128 textContent-escape
    'static/js/ai-panel.js': 1,               # :3546 strips ALL tags
    'static/js/report-core.js': 1,            # :98 re-escapes the 4 structural tags
}


@pytest.mark.parametrize('rel', SCANNED)
def test_no_unreviewed_dompurify_site_appears(rel):
    """A new DOMPurify guard must be reviewed, not merely present."""
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    text = path.read_text(encoding='utf-8')
    found = len(re.findall(r"typeof\s+DOMPurify\s*!==", text))
    expected = REVIEWED_FALLBACKS[rel]
    assert found == expected, (
        f"{rel} has {found} DOMPurify guard(s), {expected} reviewed. If you "
        f"added one, confirm its fallback escapes or strips rather than passing "
        f"content through, then update REVIEWED_FALLBACKS with a note saying "
        f"which."
    )


def test_the_scan_actually_catches_the_original_line():
    """Show me it failing: the fail-open source must be rejected.

    Without this, a regex that matches nothing would pass the test above
    forever and the guard would be decoration.
    """
    original = (
        "var raw = mdToHtml(data.content);\n"
        "var html = typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(raw) : raw;\n"
        "body.innerHTML = html;"
    )
    match = _FAIL_OPEN_TERNARY.search(original)
    assert match is not None, "the scan cannot see the defect it was written for"
    assert match.group(1) == 'raw'
    assert not _SAFE_FALLBACK.search(match.group(0))
