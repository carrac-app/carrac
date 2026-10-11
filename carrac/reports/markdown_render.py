"""Server-side markdown -> safe HTML for the daily brief's article body.

The client already has a rich renderer (``static/js/report-core.js``'s
``simpleMarkdownToHtml`` + a ``DOMPurify.sanitize`` pass) that repaints the
brief on load with bull/bear colorization and custom table styling. That
renderer is a bespoke regex pipeline, not CommonMark — porting it to Python
would mean maintaining two divergent markdown implementations.

This module exists only to give crawlers and no-JS visitors real text on
first paint (``/brief`` and ``/brief/<date>`` are the flagship long-tail SEO
surfaces). It renders a plain, safe version server-side; the existing client
script then overwrites it with the polished version once JS runs — standard
progressive enhancement, the client behavior is unchanged.
"""

import bleach
import markdown

_MD_EXTENSIONS = ['tables', 'nl2br', 'sane_lists']

# Server-side equivalent of the DOMPurify allowlist already used client-side
# (static/js/report-core.js: `DOMPurify.sanitize(html, {ADD_TAGS:['details','summary'], ...})`).
_ALLOWED_TAGS = [
    'p', 'h2', 'h3', 'strong', 'em', 'a',
    'ul', 'ol', 'li',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'code', 'hr', 'blockquote', 'br',
    'details', 'summary',
    'img',
]

_ALLOWED_ATTRS = {
    'a': ['href', 'target', 'rel'],
    'img': ['src', 'alt', 'loading'],
    'details': ['data-full-summary'],
}


def render_brief_html(content):
    """Convert stored report markdown to sanitized, safe-to-embed HTML.

    Never raises — a rendering hiccup degrades to an empty string so the
    route still 200s with the ``window.__briefData`` client-render fallback
    intact, rather than 500ing the whole page.
    """
    if not content:
        return ''
    try:
        html = markdown.markdown(content, extensions=_MD_EXTENSIONS)
        return bleach.clean(
            html,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            protocols=['http', 'https', 'mailto'],
            strip=True,
        )
    except Exception:
        return ''
