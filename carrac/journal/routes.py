"""
Journal Blueprint — Platform-grade trading journal API.

Reads, analytics, CSV export, screenshots, trade import, reopen and MFE/MAE.
Creating, editing, closing and deleting trades, sessions, playbooks,
checklists and goals is served by carrac/fastapi_app/routers/journal.py.
"""

import csv
import io
import json
import logging
import os
import re
from datetime import datetime
from functools import wraps

from flask import Blueprint, g, make_response, render_template, request, send_file, session

from carrac.auth.decorators import (
    auth_or_key_required, enforce_session_csrf, get_current_user, login_required,
)
from carrac.journal.tags import with_tag_lists
from carrac.json_response import jsonify_iso
from carrac.db_pool import adapt_query, get_conn as _pool_get_conn
from carrac.validation import ALLOWED_DIRECTIONS, parse_float
from carrac.content_db import get_content_db

logger = logging.getLogger('carrac.journal')

journal_bp = Blueprint('journal', __name__)


journal_bp.before_request(enforce_session_csrf)


def caller_id():
    """The calling user's id: the signed-in session, else the per-user API key.

    ``None`` for the shared internal key, which is bound to no user.
    """
    uid = session.get('user_id')
    if uid:
        return uid
    return getattr(g, 'api_key_user_id', None)


def journal_auth_required(f):
    """Session login or a per-user API key; 401 JSON for anyone else.

    ``auth_or_key_required`` lets the shared internal key through, but that
    key names no user and journal rows are always per-user, so it is refused.
    """
    @wraps(f)
    @auth_or_key_required
    def decorated(*args, **kwargs):
        if caller_id() is None:
            return jsonify_iso({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated


# ── Input validation ──

_ALLOWED_EXIT_REASONS = frozenset({
    'manual', 'tp_hit', 'sl_hit', 'trailing_stop', 'time_based', 'breakeven',
})
_ALLOWED_SCREENSHOT_TYPES = frozenset({'entry', 'exit', 'management', 'context'})
_MAX_SHORT_STR_LEN = 50
_MAX_IMPORT_PER_CALL = 50
_MAX_SCREENSHOTS_PER_TRADE = 10

# Predefined mistake tags (users can also use custom ones)
MISTAKE_TAGS = [
    'fomo', 'revenge', 'chasing', 'moved_stop', 'oversized',
    'early_entry', 'late_entry', 'early_exit', 'against_trend',
    'no_setup', 'boredom', 'news_trade', 'overtraded',
]


def _safe_int_param(name, default, max_val=365, min_val=1):
    """Safely parse an integer query parameter with bounds."""
    try:
        return max(min_val, min(int(request.args.get(name, default)), max_val))
    except (ValueError, TypeError):
        return default


def _sanitize_str(val, max_len=_MAX_SHORT_STR_LEN):
    """Sanitize a string field: strip, truncate, reject empty."""
    if val is None:
        return None
    s = str(val).strip()[:max_len]
    return s if s else None


def _verify_trade_ownership(db, journal_id, user_id):
    """Verify a trade belongs to the user. Returns the trade dict or None."""
    entry = db.get_journal_entry(journal_id)
    if not entry or entry.get('user_id') != user_id:
        return None
    return entry


def _safe_path_join(base_dir, rel_path):
    """Join paths safely, preventing directory traversal."""
    full = os.path.realpath(os.path.join(base_dir, rel_path))
    base = os.path.realpath(base_dir)
    if not full.startswith(base + os.sep) and full != base:
        return None
    return full


# ==================== Page Route ====================

@journal_bp.route('/journal')
@login_required
def journal_page():
    """Dedicated journal page."""
    user = get_current_user()
    return render_template('journal.html', user=user)


@journal_bp.route('/journal/guide')
@login_required
def journal_guide():
    """Journal guide — how to use the trade journal."""
    return render_template('journal-guide.html')


@journal_bp.route('/api/journal/credits')
@journal_auth_required
def get_credits():
    """Return user's AI credit balance."""
    from user_db import user_db
    user_id = caller_id()
    balance = user_db.get_ai_balance(user_id)
    return jsonify_iso({'balance_cents': balance})


@journal_bp.route('/data/journal-screenshots/<int:uid>/<int:tid>/<filename>')
@login_required
def serve_screenshot(uid, tid, filename):
    """Serve journal screenshot files with ownership check."""
    user_id = session.get('user_id')
    if uid != user_id:
        return jsonify_iso({'error': 'Forbidden'}), 403
    # Validate filename (prevent traversal)
    if not re.match(r'^[a-z]+_\d+\.jpg$', filename):
        return jsonify_iso({'error': 'Invalid filename'}), 400
    data_dir = os.environ.get('DATA_DIR', 'data')
    full_path = _safe_path_join(data_dir, os.path.join('journal-screenshots', str(uid), str(tid), filename))
    if not full_path or not os.path.isfile(full_path):
        return jsonify_iso({'error': 'Not found'}), 404
    return send_file(full_path, mimetype='image/jpeg')


# ==================== Trades CRUD ====================

@journal_bp.route('/api/journal/trades', methods=['GET'])
@journal_auth_required
def trade_list():
    """List trades with filtering support."""
    user_id = caller_id()
    db = get_content_db()

    days = _safe_int_param('days', 30, 365)
    limit = _safe_int_param('limit', 100, 500)
    include_open = request.args.get('include_open', 'true').lower() == 'true'

    # Get base data
    history = db.get_trade_history(user_id, days=days, limit=limit)
    open_trades = db.get_open_trades(user_id) if include_open else []
    stats = db.get_journal_stats(user_id, days=days)

    # Apply filters (server-side)
    asset = _sanitize_str(request.args.get('asset'), 20)
    if asset:
        asset = asset.upper()
    setup = _sanitize_str(request.args.get('setup'), _MAX_SHORT_STR_LEN)
    direction = request.args.get('direction', '')
    if direction and direction not in ALLOWED_DIRECTIONS:
        direction = ''
    has_mistakes = request.args.get('has_mistakes')
    if has_mistakes not in ('true', 'false', None):
        has_mistakes = None
    min_rr = parse_float(request.args, 'min_rr')
    max_rr = parse_float(request.args, 'max_rr')
    tags_filter = _sanitize_str(request.args.get('tags'), _MAX_SHORT_STR_LEN)

    def _matches(trade):
        if asset and trade.get('asset') != asset:
            return False
        if setup and trade.get('setup_type') != setup:
            return False
        if direction and trade.get('direction') != direction:
            return False
        if has_mistakes == 'true':
            mt = trade.get('mistake_tags')
            if not mt or mt == '[]':
                return False
        elif has_mistakes == 'false':
            mt = trade.get('mistake_tags')
            if mt and mt != '[]':
                return False
        rr = trade.get('rr_actual')
        if min_rr is not None and (rr is None or rr < min_rr):
            return False
        if max_rr is not None and (rr is None or rr > max_rr):
            return False
        if tags_filter:
            trade_tags = trade.get('tags', '[]')
            try:
                trade_tags_list = json.loads(trade_tags) if trade_tags else []
            except (json.JSONDecodeError, TypeError):
                trade_tags_list = []
            if tags_filter not in trade_tags_list:
                return False
        return True

    any_filter = asset or setup or direction or has_mistakes or min_rr is not None or max_rr is not None or tags_filter
    if any_filter:
        history = [t for t in history if _matches(t)]
        open_trades = [t for t in open_trades if _matches(t)]

    return jsonify_iso({
        'open': [with_tag_lists(t) for t in open_trades],
        'history': [with_tag_lists(t) for t in history],
        'stats': stats,
        'filters_applied': bool(any_filter),
    })


@journal_bp.route('/api/journal/trades/<int:trade_id>', methods=['GET'])
@journal_auth_required
def get_trade(trade_id):
    """Get a single trade with full detail including screenshots."""
    user_id = caller_id()
    db = get_content_db()
    trade = _verify_trade_ownership(db, trade_id, user_id)
    if not trade:
        return jsonify_iso({'error': 'Not found'}), 404

    # Attach screenshots
    screenshots = db.get_trade_screenshots(trade_id)
    trade['screenshots'] = screenshots

    # Attach checklist template if available
    if trade.get('setup_type'):
        checklist = db.get_checklist_for_setup(user_id, trade['setup_type'])
        trade['checklist_template'] = checklist

    return jsonify_iso(with_tag_lists(trade))


@journal_bp.route('/api/journal/trades/<int:trade_id>/reopen', methods=['POST'])
@journal_auth_required
def reopen_trade(trade_id):
    """Reopen a closed trade."""
    user_id = caller_id()
    db = get_content_db()
    trade = _verify_trade_ownership(db, trade_id, user_id)
    if not trade:
        return jsonify_iso({'error': 'Not found'}), 404

    success = db.reopen_trade(trade_id)
    return jsonify_iso({'ok': success})


@journal_bp.route('/api/journal/trades/import', methods=['POST'])
@journal_auth_required
def import_from_drawings():
    """Auto-import position drawings as journal entries."""
    user_id = caller_id()
    db = get_content_db()

    drawings = db.drawing_list(user_id=user_id)
    imported = 0

    # Tier gate — journal_trades_monthly cap applies to bulk import too
    # (checked per iteration).
    # This route is live: it is served via asgi.py's EXCLUDED_SUBPATHS and
    # static/js/journal-page.js calls it.
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    limits = get_tier_limits(get_request_tier())
    monthly_cap = limits.get('journal_trades_monthly', 0)
    base_count = db.count_trades_this_month(user_id)
    capped = False

    for d in drawings:
        if imported >= _MAX_IMPORT_PER_CALL:
            break
        if (base_count + imported) >= monthly_cap:
            capped = True
            break

        dtype = d.get('drawing_type', '')
        if dtype not in ('position', 'long_position', 'short_position'):
            continue
        # Skip if already linked
        existing = db.find_journal_by_drawing(d['id'])
        if existing:
            continue

        direction = 'long'
        if dtype == 'short_position':
            direction = 'short'
        elif dtype == 'long_position':
            direction = 'long'

        entry_price = d.get('price_level')
        target_price = d.get('price_level_upper')
        stop_price = None

        # Try to extract from metadata
        meta = d.get('metadata')
        if meta:
            try:
                meta_dict = json.loads(meta) if isinstance(meta, str) else meta
                if isinstance(meta_dict, dict):
                    stop_price = meta_dict.get('stop_price') or meta_dict.get('sl')
                    if not target_price:
                        target_price = meta_dict.get('target_price') or meta_dict.get('tp')
            except (json.JSONDecodeError, TypeError):
                pass

        asset = _sanitize_str(d.get('asset'), 20)
        if not asset:
            continue
        if not entry_price or entry_price <= 0:
            continue

        db.log_trade_open(
            user_id=user_id,
            asset=asset.upper(),
            timeframe=d.get('timeframe', '1D')[:5],
            direction=direction,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            drawing_id=d['id'],
            scenario_id=d.get('scenario_id'),
        )
        imported += 1

    return jsonify_iso({'imported': imported, 'capped': capped})


# ==================== Screenshots ====================

@journal_bp.route('/api/journal/trades/<int:trade_id>/screenshot', methods=['POST'])
@journal_auth_required
def capture_screenshot(trade_id):
    """Capture current chart viewport and attach to trade."""
    user_id = caller_id()
    db = get_content_db()
    trade = _verify_trade_ownership(db, trade_id, user_id)
    if not trade:
        return jsonify_iso({'error': 'Not found'}), 404

    # Enforce screenshot limit per trade
    existing = db.get_trade_screenshots(trade_id)
    if len(existing) >= _MAX_SCREENSHOTS_PER_TRADE:
        return jsonify_iso({'error': f'Maximum {_MAX_SCREENSHOTS_PER_TRADE} screenshots per trade'}), 400

    data = request.get_json() or {}
    screenshot_type = data.get('type', 'context')
    if screenshot_type not in _ALLOWED_SCREENSHOT_TYPES:
        return jsonify_iso({'error': 'Invalid screenshot type'}), 400

    from carrac.journal.screenshots import capture_journal_screenshot
    result = capture_journal_screenshot(
        db=db,
        journal_id=trade_id,
        user_id=user_id,
        asset=trade['asset'],
        timeframe=trade.get('timeframe', '1D'),
        screenshot_type=screenshot_type,
    )

    if 'error' in result:
        return jsonify_iso(result), 500

    return jsonify_iso(result), 201


@journal_bp.route('/api/journal/trades/<int:trade_id>/screenshots', methods=['GET'])
@journal_auth_required
def list_screenshots(trade_id):
    """List screenshots attached to a trade."""
    user_id = caller_id()
    db = get_content_db()
    trade = _verify_trade_ownership(db, trade_id, user_id)
    if not trade:
        return jsonify_iso({'error': 'Not found'}), 404

    screenshots = db.get_trade_screenshots(trade_id)
    return jsonify_iso(screenshots)


@journal_bp.route('/api/journal/screenshots/<int:screenshot_id>', methods=['DELETE'])
@journal_auth_required
def delete_screenshot(screenshot_id):
    """Delete a screenshot."""
    user_id = caller_id()

    # Ownership check (RAII pool conn)
    with _pool_get_conn() as conn:
        row = conn.execute(
            adapt_query('SELECT * FROM journal_screenshots WHERE id = ? AND user_id = ?'),
            (screenshot_id, user_id)
        ).fetchone()
    if not row:
        return jsonify_iso({'error': 'Not found'}), 404

    # Delete file with path traversal protection
    data_dir = os.environ.get('DATA_DIR', 'data')
    full_path = _safe_path_join(data_dir, row['file_path'])
    if full_path and os.path.isfile(full_path):
        try:
            os.remove(full_path)
        except OSError as e:
            logger.warning(f"Failed to delete screenshot file: {e}")

    with _pool_get_conn() as conn:
        conn.execute(adapt_query('DELETE FROM journal_screenshots WHERE id = ? AND user_id = ?'),
                     (screenshot_id, user_id))
    return jsonify_iso({'ok': True})


# ==================== Checklists ====================

@journal_bp.route('/api/journal/checklists', methods=['GET'])
@journal_auth_required
def list_checklists():
    """List user's checklist templates."""
    user_id = caller_id()
    db = get_content_db()
    checklists = db.get_user_checklists(user_id)
    return jsonify_iso(checklists)


# ==================== Analytics ====================

@journal_bp.route('/api/journal/analytics', methods=['GET'])
@journal_auth_required
def analytics():
    """Comprehensive journal analytics."""
    user_id = caller_id()
    db = get_content_db()
    days = _safe_int_param('days', 90, 365)
    data = db.get_journal_analytics(user_id, days=days)
    return jsonify_iso(data)


@journal_bp.route('/api/journal/analytics/playbook', methods=['GET'])
@journal_auth_required
def playbook_analytics():
    """Per-setup / archetype-tag / symbol / side / time-of-day expectancy
    breakdown across CLOSED trades.

    Paid feature — gated on TIER_LIMITS[tier]['journal_analytics'] with a
    CLOSED default (False when the key is absent, so the panel fails
    locked). The knob ships in the paid-tier matrix: free locked, paid
    tiers unlocked.
    """
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    from carrac.journal.playbook_analytics import get_playbook_analytics

    days = _safe_int_param('days', 90, 365)
    limits = get_tier_limits(get_request_tier())

    if not limits.get('journal_analytics', False):
        return jsonify_iso({
            'locked': True,
            'period_days': days,
            'reason': ('Playbook analytics (per-setup expectancy, R-multiple '
                       'distribution, and average MAE/MFE) requires a paid plan.'),
        })

    user_id = caller_id()
    db = get_content_db()
    data = get_playbook_analytics(db, user_id, days=days)
    data['locked'] = False
    return jsonify_iso(data)


# ==================== Mistake Tags ====================

@journal_bp.route('/api/journal/mistake-tags', methods=['GET'])
@journal_auth_required
def get_mistake_tags():
    """Get available mistake tags (predefined + user's custom)."""
    user_id = caller_id()

    # Get custom tags the user has used (RAII pool conn)
    with _pool_get_conn() as conn:
        rows = conn.execute(
            adapt_query("SELECT DISTINCT mistake_tags FROM trade_journal WHERE user_id = ? AND mistake_tags IS NOT NULL"),
            (user_id,)
        ).fetchall()

    custom = set()
    for row in rows:
        try:
            tags = json.loads(row['mistake_tags'])
            if isinstance(tags, list):
                for t in tags:
                    if isinstance(t, str) and len(t) <= _MAX_SHORT_STR_LEN:
                        custom.add(t)
        except (json.JSONDecodeError, TypeError):
            pass

    all_tags = sorted(set(MISTAKE_TAGS) | custom)
    return jsonify_iso({'tags': all_tags, 'predefined': MISTAKE_TAGS})


# ==================== Calendar ====================

@journal_bp.route('/api/journal/calendar', methods=['GET'])
@journal_auth_required
def calendar_data():
    """Get daily trade aggregates for a calendar month."""
    user_id = caller_id()
    db = get_content_db()

    now = datetime.now()
    year = _safe_int_param('year', now.year, 2100)
    if year < 2000:
        year = now.year
    month = _safe_int_param('month', now.month, 12)
    if month < 1:
        month = 1

    data = db.get_calendar_data(user_id, year, month)
    return jsonify_iso(data)


# ==================== Equity Curve ====================

@journal_bp.route('/api/journal/equity-curve', methods=['GET'])
@journal_auth_required
def equity_curve():
    """Get cumulative R time series for equity curve chart."""
    user_id = caller_id()
    db = get_content_db()
    days = _safe_int_param('days', 90, 365)
    curve = db.get_equity_curve(user_id, days=days)
    return jsonify_iso({'curve': curve, 'days': days})


# ==================== Sessions ====================

@journal_bp.route('/api/journal/sessions', methods=['GET'])
@journal_auth_required
def list_sessions():
    """List trading sessions."""
    user_id = caller_id()
    db = get_content_db()

    now = datetime.now()
    year = _safe_int_param('year', now.year, 2100)
    if year < 2000:
        year = now.year
    month = _safe_int_param('month', now.month, 12)
    if month < 1:
        month = 1

    sessions_list = db.get_sessions(user_id, year=year, month=month)

    # Batch-fetch trade stats for all sessions in one query (avoids N+1)
    # RAII pool conn avoids pinning a pool slot.
    if sessions_list:
        session_ids = [s['id'] for s in sessions_list]
        placeholders = ','.join('?' * len(session_ids))
        with _pool_get_conn() as conn:
            rows = conn.execute(
                f'''SELECT session_id,
                           COUNT(*) as trade_count,
                           SUM(CASE WHEN rr_actual IS NOT NULL AND rr_actual > 0 THEN 1 ELSE 0 END) as win_count,
                           SUM(CASE WHEN rr_actual IS NOT NULL AND rr_actual <= 0 THEN 1 ELSE 0 END) as loss_count,
                           COALESCE(ROUND(SUM(CASE WHEN rr_actual IS NOT NULL THEN rr_actual ELSE 0 END), 2), 0) as net_r
                    FROM trade_journal
                    WHERE session_id IN ({placeholders})
                    GROUP BY session_id''',
                session_ids
            ).fetchall()
        stats_map = {r['session_id']: dict(r) for r in rows}

        for s in sessions_list:
            st = stats_map.get(s['id'], {})
            s['trade_count'] = st.get('trade_count', 0)
            s['net_r'] = st.get('net_r', 0)
            s['win_count'] = st.get('win_count', 0)
            s['loss_count'] = st.get('loss_count', 0)

    return jsonify_iso(sessions_list)


@journal_bp.route('/api/journal/sessions/<int:session_id>/trades', methods=['GET'])
@journal_auth_required
def session_trades(session_id):
    """Get trades linked to a session."""
    user_id = caller_id()
    db = get_content_db()

    sess = db.get_session(session_id)
    if not sess or sess.get('user_id') != user_id:
        return jsonify_iso({'error': 'Not found'}), 404

    trades = db.get_session_trades(session_id)
    return jsonify_iso([with_tag_lists(t) for t in trades])


# ==================== AI Features ====================





# ==================== Playbooks ====================

@journal_bp.route('/api/journal/playbooks', methods=['GET'])
@journal_auth_required
def list_playbooks():
    """List user's playbooks."""
    user_id = caller_id()
    db = get_content_db()
    playbooks = db.get_playbooks(user_id)
    return jsonify_iso(playbooks)


@journal_bp.route('/api/journal/playbooks/<int:playbook_id>/stats', methods=['GET'])
@journal_auth_required
def playbook_stats(playbook_id):
    """Get live stats for a playbook's setup type."""
    user_id = caller_id()
    db = get_content_db()

    pb = db.get_playbook(playbook_id)
    if not pb or pb.get('user_id') != user_id:
        return jsonify_iso({'error': 'Not found'}), 404
    if not pb.get('setup_type'):
        return jsonify_iso({'trades': 0})

    stats = db.get_playbook_stats(user_id, pb['setup_type'])
    return jsonify_iso(stats)


# ==================== Goals ====================

@journal_bp.route('/api/journal/goals', methods=['GET'])
@journal_auth_required
def get_goals():
    """Get goals for a month."""
    user_id = caller_id()
    db = get_content_db()

    month = _sanitize_str(request.args.get('month'), 7)
    if not month or not re.match(r'^\d{4}-\d{2}$', month):
        month = datetime.now().strftime('%Y-%m')

    goals = db.get_goals(user_id, month)
    progress = db.get_goal_progress(user_id, month)

    return jsonify_iso({
        'goals': goals,
        'progress': progress,
        'month': month,
    })


# ==================== Export ====================

_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in a CSV cell.

    A text cell starting with = + - @ (or a tab/CR) is executed as a formula by
    Excel/Sheets/LibreOffice. Prefix a single quote so it is shown as text.
    Non-strings (numbers, None) pass through unchanged.
    """
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


@journal_bp.route('/api/journal/export/csv', methods=['GET'])
@journal_auth_required
def export_csv():
    """Export trades as CSV."""
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    if not get_tier_limits(get_request_tier()).get('data_export'):
        return jsonify_iso({'error': 'Data export requires a subscription',
                        'upgrade_required': True}), 403
    user_id = caller_id()
    db = get_content_db()
    days = _safe_int_param('days', 365, 3650)

    trades = db.export_trades_csv(user_id, days=days)
    if not trades:
        return jsonify_iso({'error': 'No trades to export'}), 404

    # Build CSV
    output = io.StringIO()
    fields = [
        'id', 'asset', 'timeframe', 'direction', 'entry_price', 'exit_price',
        'stop_price', 'target_price', 'rr_planned', 'rr_actual', 'setup_type',
        'exit_reason', 'opened_at', 'closed_at', 'size', 'fees', 'pnl_currency',
        'confidence', 'mood', 'notes', 'tags', 'mistake_tags', 'mfe', 'mae',
        'exit_efficiency', 'account_tag',
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for t in trades:
        writer.writerow({k: _csv_safe(v) for k, v in t.items()})

    resp = make_response(output.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename=journal_export_{datetime.now().strftime("%Y%m%d")}.csv'
    return resp


@journal_bp.route('/api/journal/export/json', methods=['GET'])
@journal_auth_required
def export_json():
    """Export full journal data as JSON backup."""
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    if not get_tier_limits(get_request_tier()).get('data_export'):
        return jsonify_iso({'error': 'Data export requires a subscription',
                        'upgrade_required': True}), 403
    user_id = caller_id()
    db = get_content_db()

    trades = db.export_trades_csv(user_id, days=3650)
    playbooks = db.get_playbooks(user_id)
    checklists = db.get_user_checklists(user_id)
    all_goals = db.get_all_goals(user_id)

    now = datetime.now()

    backup = {
        'exported_at': now.isoformat(),
        'trades': [with_tag_lists(t) for t in trades],
        'playbooks': playbooks,
        'checklists': checklists,
        'goals': all_goals,
    }

    resp = make_response(json.dumps(backup, default=str, indent=2))
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename=journal_backup_{now.strftime("%Y%m%d")}.json'
    return resp


# ==================== Exchange Sync ====================
#
# Read-only Binance/Hyperliquid connections + FIFO fill import. CSRF is
# already enforced blueprint-wide by enforce_session_csrf() above. Gated on the
# journal_autosync tier knob — deliberately absent from TIER_LIMITS until a
# parallel branch wires it into the paid-tier matrix (see
# carrac/journal/exchange_sync.py's module docstring), so every tier
# resolves to False (locked) until then.

def _exchange_sync_limits():
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    return get_tier_limits(get_request_tier())


@journal_bp.route('/api/journal/exchange-sync/connections', methods=['GET'])
@journal_auth_required
def list_exchange_connections():
    """List the user's exchange connections (masked — never returns secrets)."""
    from carrac.journal.exchange_connections import list_connections, to_public_dict
    from carrac.journal.exchange_sync import list_suggestions

    limits = _exchange_sync_limits()
    if not limits.get('journal_autosync', False):
        return jsonify_iso({'locked': True,
                        'reason': 'Exchange auto-sync requires a paid plan.'})

    user_id = caller_id()
    connections = [to_public_dict(c) for c in list_connections(user_id)]
    suggestions = list_suggestions(user_id, status='pending')
    return jsonify_iso({'locked': False, 'connections': connections, 'suggestions': suggestions})


@journal_bp.route('/api/journal/exchange-sync/connections', methods=['POST'])
@journal_auth_required
def connect_exchange():
    """Connect a Binance (API key/secret) or Hyperliquid (wallet address)
    account for read-only fill sync.

    Binance keys are verified read-only via the apiRestrictions endpoint
    before anything is stored — any key with withdrawal/trading/margin/
    futures permission enabled is rejected outright.
    """
    from carrac.journal.exchange_connections import (
        ALLOWED_EXCHANGES, create_connection, is_valid_hl_address, to_public_dict,
    )
    from carrac.journal.binance_auth_client import BinanceAuthClient, BinancePermissionError

    limits = _exchange_sync_limits()
    if not limits.get('journal_autosync', False):
        return jsonify_iso({'error': 'Exchange auto-sync requires a paid plan.',
                        'upgrade_required': True}), 403

    data = request.get_json() or {}
    exchange = (data.get('exchange') or '').strip().lower()
    if exchange not in ALLOWED_EXCHANGES:
        return jsonify_iso({'error': f'exchange must be one of: {", ".join(sorted(ALLOWED_EXCHANGES))}'}), 400

    label = _sanitize_str(data.get('label'), _MAX_SHORT_STR_LEN)
    user_id = caller_id()

    if exchange == 'binance':
        api_key = (data.get('api_key') or '').strip()
        api_secret = (data.get('api_secret') or '').strip()
        if not api_key or not api_secret:
            return jsonify_iso({'error': 'api_key and api_secret are required'}), 400
        try:
            BinanceAuthClient(api_key, api_secret).check_read_only()
        except BinancePermissionError as e:
            return jsonify_iso({'error': str(e)}), 400
        except Exception as e:
            logger.warning(f'exchange-sync: Binance connect verification failed: {e}')
            return jsonify_iso({
                'error': 'Could not verify this API key with Binance. '
                         'Check the key/secret and try again.'
            }), 400
        credentials = {'api_key': api_key, 'api_secret': api_secret}
    else:  # hyperliquid
        address = (data.get('address') or '').strip()
        if not is_valid_hl_address(address):
            return jsonify_iso({
                'error': 'address must be a valid 0x-prefixed 40-hex-character wallet address'
            }), 400
        credentials = {'address': address}

    from carrac.secret_box import EncryptionUnavailable
    try:
        conn = create_connection(user_id, exchange, credentials, label=label)
    except EncryptionUnavailable:
        # Already logged at ERROR by the crypto helper (→ Bugsink).
        return jsonify_iso({
            'error': 'Exchange credentials cannot be stored right now: the '
                     'server has no encryption key configured. Nothing was saved.'
        }), 503
    return jsonify_iso(to_public_dict(conn)), 201


@journal_bp.route('/api/journal/exchange-sync/connections/<int:connection_id>', methods=['DELETE'])
@journal_auth_required
def disconnect_exchange(connection_id):
    """Hard-purge a connection (credentials + raw fill ledger + suggestions)."""
    from carrac.journal.exchange_connections import delete_connection

    user_id = caller_id()
    ok = delete_connection(connection_id, user_id)
    if not ok:
        return jsonify_iso({'error': 'Not found'}), 404
    return jsonify_iso({'ok': True})


@journal_bp.route('/api/journal/exchange-sync/connections/<int:connection_id>/sync-now', methods=['POST'])
@journal_auth_required
def sync_exchange_connection_now(connection_id):
    """Manually trigger one connection's sync (in addition to the periodic
    15-minute background pass) — mainly useful right after connecting."""
    from carrac.journal.exchange_connections import get_connection, mark_sync_error
    from carrac.journal.exchange_sync import sync_connection

    limits = _exchange_sync_limits()
    if not limits.get('journal_autosync', False):
        return jsonify_iso({'error': 'Exchange auto-sync requires a paid plan.',
                        'upgrade_required': True}), 403

    user_id = caller_id()
    conn = get_connection(connection_id, user_id)
    if not conn:
        return jsonify_iso({'error': 'Not found'}), 404
    if conn['status'] == 'error':
        return jsonify_iso({
            'error': 'This connection was disabled after repeated failures — '
                     'reconnect with fresh credentials to retry.'
        }), 400

    try:
        result = sync_connection(conn)
    except Exception as e:
        mark_sync_error(connection_id, str(e))
        logger.warning(f'exchange-sync: manual sync failed for connection {connection_id}: {e}')
        return jsonify_iso({'error': f'Sync failed: {e}'}), 502

    return jsonify_iso(result)


@journal_bp.route('/api/journal/exchange-sync/suggestions/<int:suggestion_id>/resolve', methods=['POST'])
@journal_auth_required
def resolve_exchange_suggestion(suggestion_id):
    """Resolve a dedupe suggestion: action='import' logs it as a real (tagged)
    journal trade; action='skip' just dismisses it."""
    from carrac.journal.exchange_sync import resolve_suggestion

    data = request.get_json() or {}
    action = (data.get('action') or '').strip().lower()
    if action not in ('import', 'skip'):
        return jsonify_iso({'error': 'action must be "import" or "skip"'}), 400

    user_id = caller_id()
    result = resolve_suggestion(suggestion_id, user_id, action)
    if result is None:
        return jsonify_iso({'error': 'Not found'}), 404
    return jsonify_iso(result)
