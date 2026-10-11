"""Public brief permalinks + archive index — Task B.

Honest-permalink design: ``/brief/<date>``
renders the STORED ``general`` report of that calendar date in a simple
reader layout. It never shows live bands — those would silently overlay
TODAY's market data on an old report, which is dishonest. ``/brief`` (see
``carrac.reports.routes.brief``) stays the live cockpit; this module is
strictly the read-only archive/permalink surface next to it.

Same idioms as ``/brief``:
  * ``carrac.extensions.limiter`` rate limiting.
  * A Redis page-cache for the dated route (never fatal — any cache error
    falls straight through to a live render).
  * The exact same operator-neutral shaping ``/brief``'s report blob uses
    (``carrac.reports.sanitize.general_report_public`` — operator-section
    strip plus the Deribit prose scrub — + the
    ``PERSONAL_SECTION_START`` marker truncation) — never re-implemented,
    only reproduced for a specific date instead of "latest" (see
    ``carrac.reports.routes._gather_general_report_json``, which this
    mirrors but does not import, since that helper always fetches the
    latest report and this module's file ownership is read-only on
    ``carrac/reports/routes.py``).

This blueprint is registered in ``carrac/__init__.py`` next to
``reports_bp`` (registration only — no route logic there).
"""

import logging
import re
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, g, render_template

from carrac.extensions import limiter

logger = logging.getLogger('carrac.reports.brief_archive')

brief_archive_bp = Blueprint('brief_archive', __name__)

_REPORT_TYPE = 'general'
_PERSONAL_MARKER = '<!-- PERSONAL_SECTION_START -->'

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Cached brief pages hold this in place of the rendering request's CSP nonce;
# every response substitutes its own nonce, so the inline scripts match the
# response's CSP header.
CSP_NONCE_PLACEHOLDER = '__BRIEF_CSP_NONCE__'

# v3 — see _BRIEF_CACHE_KEY in carrac/reports/routes.py. These entries live
# 6h, so without the bump a permalink could serve stale HTML well into the
# evening after the promote.
_DATED_CACHE_PREFIX = 'brief:date:html:v3:'
_DATED_CACHE_TTL = 6 * 3600  # 6h — a dated page's content is immutable once
                              # the day's report exists; short TTL is only to
                              # cover the (rare) case a report is regenerated.

_ARCHIVE_LIMIT = 30


def _valid_iso_date(date_str):
    """True for a syntactically valid, non-future ISO calendar date.

    Rejects malformed input (path-traversal-ish junk, wrong separators,
    impossible dates) AND any future date — a dated permalink is only ever
    real, already-published content; "tomorrow" can never have a report.
    """
    if not date_str or not _DATE_RE.match(date_str):
        return False
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return False
    return d <= datetime.now(timezone.utc).date()


def _sanitize_general_content(content):
    """The same two-step operator-neutral shaping
    ``carrac.reports.routes._gather_general_report_json`` applies to /brief's
    report blob: truncate at the personal-section splice marker (defense in
    depth — the general report never gets one by design), then strip any
    operator-private sections and Deribit-derived prose via the shared
    sanitizer.

    Older reports in this archive were written
    while the Deribit block was in the briefing, so the licensing scrub matters
    far more here than on /brief — that page self-heals daily as new reports
    land, and these permalinks never do."""
    from carrac.reports.sanitize import general_report_public
    return general_report_public(_strip_personal(content))


def _strip_personal(content):
    """The shared report, without any inline per-user section."""
    content = content or ''
    idx = content.find(_PERSONAL_MARKER)
    return content[:idx] if idx != -1 else content


def _gather_general_report_by_date(date_str):
    """Same shaping as ``_gather_general_report_json`` (see module
    docstring), for one calendar date. Never raises — returns None on any
    failure (missing report, DB hiccup) so the route can 404 cleanly."""
    try:
        from carrac.content_db import get_content_db
        from carrac.reports.markdown_render import render_brief_html

        db = get_content_db()
        report = db.get_report_by_date(report_type=_REPORT_TYPE, date=date_str)
        if not report:
            return None
        content = _sanitize_general_content(report.get('content'))
        return {
            'content': content,
            'html': render_brief_html(content),
            'id': report.get('id'),
        }
    except Exception:
        logger.exception("Failed to gather general report for %s", date_str)
        return None


def _tldr_line(content, summary):
    """A short archive-row teaser: the stored TL;DR summary, falling back to
    the first substantial paragraph of the (already-sanitized) content.
    Mirrors the fallback chain in
    ``carrac.dashboard.routes._landing_report_teaser_v5`` (kept local — that
    helper is single "latest report" scoped, this needs a per-row version)."""
    tldr = (summary or '').strip()
    if not tldr:
        for para in (content or '').split('\n\n'):
            p = para.strip()
            if len(p) >= 40 and not p.startswith('#'):
                tldr = p
                break
    if not tldr:
        return ''
    tldr = re.sub(r'^([-*•]|\d+[.)])\s+', '', tldr).strip()
    tldr = re.sub(r'\*\*([^*]+)\*\*', r'\1', tldr)
    tldr = re.sub(r'[#`*]', '', tldr).strip()
    if len(tldr) > 200:
        tldr = tldr[:197].rsplit(' ', 1)[0] + '…'
    return tldr


def list_recent_general_reports(limit=_ARCHIVE_LIMIT):
    """Last ``limit`` general reports, newest first: ``[{date, tldr}]``.

    Uses only the public ``ContentDatabase`` API (``get_all_reports`` +
    ``get_report_by_id``) rather than reaching into a raw connection — one
    extra point-lookup per row, bounded by ``limit`` and cheap (primary-key
    reads). Never raises — degrades to ``[]`` so the archive page renders
    (empty state) instead of 500ing on a DB hiccup.
    """
    try:
        from carrac.content_db import get_content_db
        from carrac.reports.sanitize import report_text_for_viewer

        db = get_content_db()
        rows = db.get_all_reports(report_type=_REPORT_TYPE, limit=limit)
        out = []
        for row in rows:
            full = db.get_report_by_id(row.get('id'))
            if not full:
                continue
            date_str = str(full.get('created_at') or '')[:10]
            if not date_str:
                continue
            shown = report_text_for_viewer(
                {**full, 'content': _strip_personal(full.get('content'))},
                'general', operator=False)
            out.append({'date': date_str,
                        'tldr': _tldr_line(shown['content'], shown['summary'])})
        return out
    except Exception:
        logger.exception("Failed to list recent general reports")
        return []


@brief_archive_bp.route('/brief/archive')
@limiter.limit("30 per minute")
def brief_archive():
    """Public index of the last 30 general reports: date + tldr line, each
    linking to its ``/brief/<date>`` permalink. Anonymous, read-only, no
    live market data — a directory page only."""
    from morning_engine.config import reports_paused
    reports = list_recent_general_reports(_ARCHIVE_LIMIT)
    return render_template('brief_archive.html', reports=reports, paused=reports_paused())


@brief_archive_bp.route('/brief/<date>')
@limiter.limit("30 per minute")
def brief_dated(date):
    """Honest permalink: the STORED general report for ``date`` in a simple
    reader layout — never live bands (see module docstring). Page-cached in
    Redis per date (key ``brief:date:html:v3:<date>``, TTL 6h); any cache
    error is non-fatal and falls straight through to a live render.
    """
    if not _valid_iso_date(date):
        abort(404)

    cache_key = _DATED_CACHE_PREFIX + date
    r = None
    try:
        from carrac.redis_cache import get_redis
        r = get_redis()
        if r:
            cached = r.get(cache_key)
            if cached:
                if isinstance(cached, bytes):
                    cached = cached.decode('utf-8', 'replace')
                return Response(
                    cached.replace(CSP_NONCE_PLACEHOLDER, getattr(g, 'csp_nonce', '')),
                    mimetype='text/html',
                )
    except Exception:
        r = None

    report = _gather_general_report_by_date(date)
    if not report:
        abort(404)

    html = render_template(
        'brief_reader.html', date=date, report=report, csp_nonce=CSP_NONCE_PLACEHOLDER,
    )

    try:
        if r:
            r.setex(cache_key, _DATED_CACHE_TTL, html)
    except Exception:
        pass

    return Response(
        html.replace(CSP_NONCE_PLACEHOLDER, getattr(g, 'csp_nonce', '')),
        mimetype='text/html',
    )
