"""MCP tools: Report tools.

  - report_get_raw: full structured report (HTML, summary, metadata); non-operator
    callers get the filtered cut
  - report_get_section: extract a single section by header
  - report_get_diff: structural diff between two report dates
  - report_generate_conditional: lightweight intraday brief built from
    cached scanner + regime + recent transitions, attributed to a triggering
    condition.
"""

import logging
import re
from datetime import datetime, timezone

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError

logger = logging.getLogger('carrac.mcp.tools.reports')


def _extract_section(report_text, section_name):
    """Extract a single ## section body from a markdown report."""
    if not report_text or not section_name:
        return ''
    pattern = rf"^## {re.escape(section_name)}\s*\n(.*?)(?=^## |\Z)"
    m = re.search(pattern, report_text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ''


def _list_sections(report_text):
    """Return ordered list of ## section headers in a report."""
    if not report_text:
        return []
    return re.findall(r"^## (.+?)\s*$", report_text, re.MULTILINE)


def _caller_is_free():
    """True if the current MCP caller is on a tier with no personal report
    (i.e. a free-tier consumer). Fails CLOSED on error is NOT desirable here —
    a serving check, not a gate — so an unknown tier is treated as NOT free
    (paid), matching the previous resolve behaviour. The leak is closed by the
    fallback gate + sanitizer below, which only loosen for paid callers."""
    try:
        from carrac.mcp.runtime import get_mcp_caller_tier
        from carrac.auth.decorators import get_tier_limits
        pr = get_tier_limits(get_mcp_caller_tier()).get('personal_report')
        return (not pr) or pr == 'none'
    except Exception:
        return False


from carrac.compliance.deribit_gate import strip_deribit_columns  # noqa: E402


def _caller_is_admin():
    """True only for the operator (admin tier). Fails CLOSED — an unknown or
    unresolvable tier is NOT the operator."""
    try:
        from carrac.mcp.runtime import get_mcp_caller_tier
        return get_mcp_caller_tier() == 'admin'
    except Exception:
        return False


def _report_row_for_caller(report, rtype):
    """The stored report row as the current MCP caller may see it.

    Applies ``report_text_for_viewer``, the policy every report-serving
    handler uses: the operator (admin tier) gets the row unchanged; everyone
    else gets filtered ``content``/``html``/``summary``, no ``audio_script``,
    no ``source_briefing`` (the raw briefing the report was written from, which
    carries the operator's positions and private inputs) and a ``metadata``
    reduced to the publishable ``x_candidates``.
    """
    out = dict(report)
    if _caller_is_admin():
        return out
    import json
    from carrac.reports.sanitize import public_x_candidates, report_text_for_viewer

    out.update(report_text_for_viewer(report, rtype, operator=False))
    if out.get('html'):
        out['html'] = report_text_for_viewer(
            {**report, 'content': report['html']}, rtype, operator=False)['content']
    out.pop('source_briefing', None)
    meta = report.get('metadata')
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = None
    candidates = meta.get('x_candidates') if isinstance(meta, dict) else None
    out['metadata'] = ({'x_candidates': public_x_candidates(candidates)}
                       if isinstance(candidates, list) else {})
    return out


def _resolve_report_type_for_caller(rtype):
    """Map the requested report type to what the caller is entitled to.

    The operator's personal "morning" report (positions/alerts woven in) is
    ADMIN-ONLY: every other caller — paid included — resolves to the
    operator-neutral shared "general" report. Position-tracking reports return
    as a per-user paid feature, not as a window into the operator's book.
    Non-morning types pass through unchanged.
    """
    if rtype != 'morning':
        return rtype
    return rtype if _caller_is_admin() else 'general'


# Per-type staleness threshold (hours) above which a report is treated as
# "the generating pipeline has stalled" rather than merely between its
# normal cycles. Mirrors carrac/admin/health_checks.py's `_REPORT_CADENCE`
# / weekend-tolerant morning threshold (kept as local constants rather than
# a cross-module import — that module is the admin-dashboard subsystem, this
# is the MCP surface; sync manually if either changes).
_STALE_RED_HOURS = {
    'morning':    90,   # Mon-Fri only schedule; weekend-tolerant
    'general':    54,
    'afternoon':  54,
    'weekly':     240,  # once/week (Fri) + one missed-cycle allowance
    'week_ahead': 240,
}
_DEFAULT_STALE_RED_HOURS = 54

# Fallback pool for a stale/missing report: shared, operator-neutral-by-design
# report types only. 'morning' is deliberately excluded — it is the
# operator-personal pipeline, gated separately by
# _resolve_report_type_for_caller / allow_morning_fallback, and must never be
# reached via the freshness fallback.
SHARED_REPORT_TYPES = ('general', 'afternoon', 'weekly')


def _report_age_hours(report):
    """Age of a report row in hours, or None if created_at is missing/unparseable.

    Naive-UTC comparison, matching the convention already used in
    carrac/admin/health_checks.py.
    """
    if not report:
        return None
    created_raw = report.get('created_at')
    if not created_raw:
        return None
    if isinstance(created_raw, datetime):
        created = created_raw.replace(tzinfo=None)
    else:
        try:
            created = datetime.fromisoformat(str(created_raw).replace('Z', ''))
        except (ValueError, TypeError):
            return None
        created = created.replace(tzinfo=None)
    return (datetime.utcnow() - created).total_seconds() / 3600.0


def _is_stale(report, rtype):
    """True when ``report`` is missing, has no legible created_at, or is
    older than its type's red_h threshold — i.e. the generating pipeline has
    likely stalled (e.g. upstream LLM API exhaustion) rather
    than the report merely being between its normal cycles. An unknown age
    is treated as stale: we do not vouch for freshness we cannot verify.
    """
    if not report:
        return True
    age = _report_age_hours(report)
    if age is None:
        return True
    threshold = _STALE_RED_HOURS.get(rtype, _DEFAULT_STALE_RED_HOURS)
    return age > threshold


def _freshest_shared_report(db, exclude=()):
    """Return (report, rtype) for whichever SHARED_REPORT_TYPES row is both
    present and NOT itself stale, skipping types in ``exclude``.

    Returns (None, None) when every shared type is missing or itself stale —
    callers must fall through to whatever they already had rather than
    fabricate a substitute.
    """
    best = best_type = None
    best_age = None
    for t in SHARED_REPORT_TYPES:
        if t in exclude:
            continue
        try:
            r = db.get_latest_report(report_type=t)
        except Exception:
            r = None
        if not r or _is_stale(r, t):
            continue
        age = _report_age_hours(r)
        if age is None:
            continue
        if best_age is None or age < best_age:
            best, best_type, best_age = r, t, age
    return best, best_type


def _fetch_report_with_fallback(db, rtype, allow_morning_fallback=True,
                                 allow_freshness_fallback=True):
    """Fetch latest report of rtype, applying two independent fallbacks.

    1. Existence: when ``rtype`` is 'general' and none exists yet, fall back
       to the shared 'morning' report so PAID callers (who asked for general
       explicitly) still get content. Gated by ``allow_morning_fallback`` —
       it MUST be False for free callers, because the morning report carries
       the operator's positions/alerts and falling back would leak them.

    2. Freshness: report pipelines can go silently dead (e.g. an exhausted
       upstream LLM API budget) while db.get_latest_report() keeps returning
       the last row it ever wrote, with nothing to signal the pipeline
       stalled. When the resolved report is missing or stale (see
       ``_is_stale``), and a fresher SHARED report exists
       (general/afternoon/weekly — never 'morning'), prefer the fresher one.
       If every shared type is also stale/missing, the original
       stale/absent result is returned unchanged rather than fabricated.

    Returns (report, rtype) — rtype reflects whichever report was actually
    returned, which may differ from the rtype passed in. Callers should
    compare against the caller's original request to detect a fallback and
    surface it (e.g. requested_type / is_fallback in the response payload).
    """
    report = db.get_latest_report(report_type=rtype)
    if not report and rtype == 'general' and allow_morning_fallback:
        rtype = 'morning'
        report = db.get_latest_report(report_type=rtype)

    if allow_freshness_fallback and _is_stale(report, rtype):
        fresher, fresher_type = _freshest_shared_report(db, exclude={rtype})
        if fresher is not None:
            report, rtype = fresher, fresher_type

    return report, rtype


# ── report_get_raw ─────────────────────────────────────

@mcp_tool(
    'report_get_raw',
    'Return the latest market report as raw structured data (id, type, html, '
    'summary, assets_included, generation_time_ms, created_at) — preferred for '
    'AI re-synthesis or downstream piping. The operator-personal morning '
    'report is admin-only; all other tiers receive the shared "general" '
    'market overview. FRESHNESS FALLBACK: if the resolved report type has no '
    'row, or its latest row is stale (its generating pipeline has stalled — '
    'e.g. no new "morning" row for weeks), this substitutes the freshest '
    'available SHARED report (general/afternoon/weekly) instead of silently '
    'returning a dead pipeline\'s last output; "morning" is never itself a '
    'fallback target. The response always carries requested_type (what you '
    'asked for) and is_fallback (whether you got something else instead), '
    'plus is_stale/age_hours for the report actually returned — freshness is '
    'reported, never assumed, so an LLM can tell a personalized report from a '
    'shared one and a fresh one from an old one. Also: weekly / afternoon. '
    'Default: morning (resolves to general for non-admin callers).',
    {
        'type': 'object',
        'properties': {
            'type': {
                'type': 'string',
                'enum': ['morning', 'general', 'weekly', 'afternoon'],
                'description': 'Report type (default morning; resolves to general for non-admin)',
            },
        },
    },
    min_tier='free',
)
def report_get_raw(arguments):
    requested_type = arguments.get('type') or 'morning'
    rtype = requested_type
    if rtype not in ('morning', 'general', 'weekly', 'afternoon'):
        raise ToolError(f'Unknown report type: {rtype}')
    is_free = _caller_is_free()
    rtype = _resolve_report_type_for_caller(rtype)
    try:
        from carrac.content_db import get_content_db
        db = get_content_db()
        # Free callers must never fall back to the operator 'morning' report.
        report, rtype = _fetch_report_with_fallback(
            db, rtype, allow_morning_fallback=not is_free)
    except Exception as e:
        raise ToolError(f'Failed to fetch report: {e}')

    if not report:
        raise ToolError(f'No {rtype} report found')

    age_hours = _report_age_hours(report)
    is_stale = _is_stale(report, rtype)

    # Non-operator callers get the serving policy's cut of the row.
    out = _report_row_for_caller(report, rtype)
    # Normalize timestamps for JSON serialization.
    for tk in ('created_at',):
        v = out.get(tk)
        if hasattr(v, 'isoformat'):
            out[tk] = v.isoformat()

    # Make the substitution unambiguous: what was asked for vs what was
    # served, and whether even what was served is itself stale. Never
    # fabricate freshness — these reflect the real row returned.
    out['requested_type'] = requested_type
    out['is_fallback'] = rtype != requested_type
    out['is_stale'] = is_stale
    out['age_hours'] = round(age_hours, 1) if age_hours is not None else None
    return {'report': out}


# ── report_get_section ─────────────────────────────────

@mcp_tool(
    'report_get_section',
    'Extract a single section from the morning/weekly/afternoon report by '
    'header (e.g. "Action Items", "Devil\'s Advocate", "Foresight Impact"). '
    'Lighter than report_get_raw — useful when Copilot only needs '
    'one part. FRESHNESS FALLBACK (no explicit date given): if the resolved '
    'type has no row, or its latest row is stale (pipeline stalled), '
    'substitutes the freshest available SHARED report (general/afternoon/'
    'weekly) — "morning" is never itself a fallback target, and an explicit '
    '`date` is always honored as-is (no substitution). Returns {section, '
    'body, all_sections, report_id, created_at, type, requested_type, '
    'is_fallback, is_stale, age_hours} — requested_type/is_fallback tell you '
    'whether what you asked for is what you got; is_stale/age_hours tell you '
    'whether even the section returned is itself old.',
    {
        'type': 'object',
        'properties': {
            'section': {
                'type': 'string',
                'description': 'Section header to extract (case-sensitive, no leading "## ")',
            },
            'type': {
                'type': 'string',
                'enum': ['morning', 'general', 'weekly', 'afternoon', 'week_ahead'],
                'description': 'Report type (default morning; resolves to general for non-admin)',
            },
            'date': {
                'type': 'string',
                'description': 'ISO date YYYY-MM-DD; default = latest',
            },
        },
        'required': ['section'],
    },
    min_tier='free',
)
def report_get_section(arguments):
    section = (arguments.get('section') or '').strip()
    if not section:
        raise ToolError('section is required')
    requested_type = arguments.get('type') or 'morning'
    rtype = requested_type
    if rtype not in ('morning', 'general', 'weekly', 'afternoon', 'week_ahead'):
        raise ToolError(f'Unknown report type: {rtype}')
    is_free = _caller_is_free()
    rtype = _resolve_report_type_for_caller(rtype)
    target_date = arguments.get('date')

    try:
        from carrac.content_db import get_content_db
        db = get_content_db()
        if target_date:
            try:
                # An explicit date is a pinned request — honored as-is, no
                # freshness substitution (the caller asked for THAT date).
                report = db.get_report_by_date(report_type=rtype, date=target_date)
            except AttributeError:
                # get_report_by_date absent on older DB shape — fall back to latest.
                report, rtype = _fetch_report_with_fallback(
                    db, rtype, allow_morning_fallback=not is_free)
        else:
            # Free callers must never fall back to the operator 'morning' report.
            report, rtype = _fetch_report_with_fallback(
                db, rtype, allow_morning_fallback=not is_free)
    except Exception as e:
        raise ToolError(f'Failed to fetch report: {e}')

    if not report:
        raise ToolError(f'No {rtype} report found')

    age_hours = _report_age_hours(report)
    is_stale = _is_stale(report, rtype)

    # A non-operator caller can never extract operator-private content: the
    # text is filtered before the lookup, so it resolves to "section not found".
    view = _report_row_for_caller(report, rtype)
    text = view.get('html') or view.get('content') or ''
    body = _extract_section(text, section)
    sections = _list_sections(text)
    if not body and sections:
        # Suggest similar headers to help the caller.
        raise ToolError(
            f'Section {section!r} not found. Available: {sections}'
        )
    created = report.get('created_at')
    if hasattr(created, 'isoformat'):
        created = created.isoformat()
    return {
        'section': section,
        'body': body,
        'all_sections': sections,
        'report_id': report.get('id'),
        'created_at': created,
        'type': rtype,
        # Make the substitution unambiguous: what was asked for vs what was
        # served, and whether even what was served is itself stale. Never
        # fabricate freshness — these reflect the real row returned.
        'requested_type': requested_type,
        'is_fallback': rtype != requested_type,
        'is_stale': is_stale,
        'age_hours': round(age_hours, 1) if age_hours is not None else None,
    }


# ── report_get_diff ────────────────────────────────────────────────

@mcp_tool(
    'report_get_diff',
    'Structural diff between two saved reports on different dates. '
    'Returns sections_added, sections_removed, sections_changed (similarity < 0.85), '
    'and sections_unchanged — each with a per-section similarity score (0–1). '
    'Also returns total_chars_a/b so you can judge how much content changed overall. '
    '"yesterday"/"today" shorthands are accepted in addition to ISO YYYY-MM-DD dates. '
    'Supports types: morning, weekly, afternoon, week_ahead (default morning). '
    'Use this for "what changed since yesterday" retrospectives or to detect '
    'regime shifts between two report dates before diving into section text. '
    'Lighter follow-up: report_get_section on a specific changed section. '
    'NOT a text-level diff — similarity is computed via SequenceMatcher ratio '
    '(falls back to length-ratio proxy for sections > 50 KB). '
    'min_tier=member.',
    {
        'type': 'object',
        'properties': {
            'date_a': {
                'type': 'string',
                'description': 'Older date YYYY-MM-DD or "yesterday"',
            },
            'date_b': {
                'type': 'string',
                'description': 'Newer date YYYY-MM-DD or "today"',
            },
            'type': {
                'type': 'string',
                'enum': ['morning', 'weekly', 'afternoon', 'week_ahead'],
                'description': 'Report type (default morning)',
            },
        },
        'required': ['date_a', 'date_b'],
    },
    min_tier='member',
)
def report_get_diff(arguments):
    from datetime import date, timedelta

    def _resolve(d):
        d = (d or '').strip().lower()
        if d in ('today', ''):
            return date.today().isoformat()
        if d == 'yesterday':
            return (date.today() - timedelta(days=1)).isoformat()
        return d

    date_a = _resolve(arguments.get('date_a'))
    date_b = _resolve(arguments.get('date_b'))
    rtype = arguments.get('type') or 'morning'
    if rtype not in ('morning', 'weekly', 'afternoon', 'week_ahead'):
        raise ToolError(f'Unknown report type: {rtype}')

    try:
        from carrac.content_db import get_content_db
        db = get_content_db()
        try:
            report_a = db.get_report_by_date(report_type=rtype, date=date_a)
            report_b = db.get_report_by_date(report_type=rtype, date=date_b)
        except AttributeError:
            raise ToolError(
                'report_get_diff requires get_report_by_date in DB layer; '
                'not available in this build'
            )
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'Failed to fetch reports: {e}')

    if not report_a:
        raise ToolError(f'No {rtype} report for date_a={date_a}')
    if not report_b:
        raise ToolError(f'No {rtype} report for date_b={date_b}')

    view_a = _report_row_for_caller(report_a, rtype)
    view_b = _report_row_for_caller(report_b, rtype)
    text_a = view_a.get('html') or view_a.get('content') or ''
    text_b = view_b.get('html') or view_b.get('content') or ''
    sec_a = set(_list_sections(text_a))
    sec_b = set(_list_sections(text_b))

    added = sorted(sec_b - sec_a)
    removed = sorted(sec_a - sec_b)
    common = sorted(sec_a & sec_b)

    # Per-section similarity (cheap: ratio of unchanged char count).
    def _similarity(a, b):
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        # Sequence-matcher ratio is O(n^2); for large reports use length proxy
        if len(a) + len(b) > 50000:
            return min(len(a), len(b)) / max(len(a), len(b))
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio()

    changed = []
    unchanged = []
    for s in common:
        sim = _similarity(_extract_section(text_a, s), _extract_section(text_b, s))
        if sim < 0.85:
            changed.append({'section': s, 'similarity': round(sim, 3)})
        else:
            unchanged.append({'section': s, 'similarity': round(sim, 3)})

    return {
        'type': rtype,
        'date_a': date_a,
        'date_b': date_b,
        'sections_added': added,
        'sections_removed': removed,
        'sections_changed': changed,
        'sections_unchanged': unchanged,
        'total_chars_a': len(text_a),
        'total_chars_b': len(text_b),
    }


# ── report_generate_conditional ───────────────────────────────

@mcp_tool(
    'report_generate_conditional',
    'Assemble a lightweight intraday context brief attributed to a named triggering '
    'condition — WITHOUT invoking the full morning-report LLM pipeline. '
    'Pulls three live data sources synchronously: '
    '(1) cached scanner verdicts for the focus assets (best_verdict, long/short totals, price); '
    '(2) derivatives regime classification for BTC (regime, confidence, description); '
    '(3) recent indicator state transitions for the focus assets (up to 15 events, '
    'min_significance=1, sorted newest-first). '
    'Returns {condition, generated_at, assets, scanner, macro, derivatives_regime, '
    'recent_transitions}. Data sources that are unavailable are silently omitted. '
    'SIDE EFFECT: none — read-only assembly from caches/DB. '
    'Up to 5 assets supported (extras silently truncated). Default: BTC,ETH,SOL. '
    'Use when an alert fires or a regime flip is detected and you want instant '
    'snapshot context without waiting for the full morning report cycle. '
    'For richer historical narrative use report_get_raw or report_get_compact. '
    'min_tier=premium.',
    {
        'type': 'object',
        'properties': {
            'condition': {
                'type': 'string',
                'description': 'Free-text label describing what triggered this brief (e.g. "BTC bb_squeeze fired", "derivatives regime flipped bearish")',
            },
            'assets': {
                'type': 'string',
                'description': 'Comma-separated asset symbols to focus on (max 5, default: BTC,ETH,SOL)',
            },
        },
        'required': ['condition'],
    },
    min_tier='premium',
)
def report_generate_conditional(arguments):
    condition = (arguments.get('condition') or '').strip()
    if not condition:
        raise ToolError('condition is required')

    assets_str = arguments.get('assets') or 'BTC,ETH,SOL'
    assets = [a.strip().upper() for a in assets_str.split(',') if a.strip()][:5]

    out = {
        'condition': condition,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'assets': assets,
    }

    # Scanner verdicts for the focus assets.
    scanner_view = []
    try:
        from carrac.caches import get_cached_scanner
        scan = get_cached_scanner() or {}
        scanner_view = [
            {
                'symbol': r.get('symbol'),
                'price': r.get('price'),
                'best_verdict': r.get('best_verdict'),
                'long_total': (r.get('long') or {}).get('total'),
                'short_total': (r.get('short') or {}).get('total'),
            }
            for r in (scan.get('results') or [])
            if r.get('symbol') in assets
        ]
        out['macro'] = scan.get('macro') or {}
    except Exception as e:
        logger.debug(f'Scanner unavailable for brief: {e}')
    out['scanner'] = scanner_view

    # Derivatives regime (BTC primary).
    try:
        from carrac.db_pool import get_quant_conn as get_conn
        # classify_asset_row owns the raw-column →
        # classifier-key mapping; raw names dropped three features.
        from quant.engine.derivatives_regime import classify_asset_row
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT basis_pct, skew_25d, dvol, iv_rv_premium,
                       options_put_call_ratio, term_structure_slope,
                       gex_net, gex_flip_price
                FROM quant.derivatives WHERE asset = 'BTC'
                ORDER BY ts DESC LIMIT 1
            """)
            row = cur.fetchone()
            # Strip Deribit columns before classifying — the
            # label is derived data too. With them gone the classifier
            # usually has nothing to work with, so the block is omitted
            # rather than emitted with a meaningless 'unknown'.
            row = strip_deribit_columns(row)
            cls = None
            if row:
                metrics = {k: float(v) for k, v in row.items() if v is not None}
                cls = classify_asset_row(metrics) if metrics else None
            if cls:
                out['derivatives_regime'] = {
                    'regime': cls.get('regime'),
                    'confidence': cls.get('confidence'),
                    'description': cls.get('description'),
                }
    except Exception as e:
        logger.debug(f'Regime unavailable for brief: {e}')

    # Recent transitions for the focus assets.
    try:
        from carrac.indicator_state import get_recent_transitions
        events = []
        for sym in assets:
            events.extend(get_recent_transitions(limit=10, symbol=sym, min_significance=1))
        events.sort(key=lambda e: e.get('ts') or '', reverse=True)
        out['recent_transitions'] = events[:15]
    except Exception as e:
        logger.debug(f'Transitions unavailable for brief: {e}')

    return out
