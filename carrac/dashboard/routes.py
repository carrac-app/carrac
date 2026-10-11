"""
Dashboard routes: main page, standalone chart, cache stats and custom formulas.
"""

import os
import logging
from pathlib import Path

from carrac.site_url import site_base_url
from flask import Blueprint, g, render_template, jsonify, request, session, Response, url_for, current_app, redirect, abort

import re
from carrac.auth.decorators import login_required, auth_or_key_required, enforce_session_csrf, get_current_user, get_request_tier, get_tier_limits, tier_required
from carrac.extensions import limiter
from user_db import PreferencesWriteError, user_db
from changelog import CHANGELOG

logger = logging.getLogger('carrac.dashboard')

dashboard_bp = Blueprint('dashboard', __name__)


dashboard_bp.before_request(enforce_session_csrf)


_SW_VERSION_TOKEN = '__APP_VERSION__'


@dashboard_bp.route('/service-worker.js')
def root_service_worker():
    """Serve service worker from root scope so it controls all pages.

    The file's cache names carry a version token; it is replaced with the
    running APP_VERSION so every release gets fresh caches.
    """
    from carrac.version import APP_VERSION
    source = (Path(current_app.static_folder) / 'service-worker.js').read_text(encoding='utf-8')
    return source.replace(_SW_VERSION_TOKEN, APP_VERSION), 200, {
        'Content-Type': 'application/javascript',
        'Service-Worker-Allowed': '/',
        'Cache-Control': 'no-cache, must-revalidate',
    }


@dashboard_bp.route('/googleda32e4dd9422762a.html')
def google_site_verification():
    """Google Search Console site-verification file (URL-prefix property)."""
    return Response(
        'google-site-verification: googleda32e4dd9422762a.html',
        mimetype='text/html',
    )


@dashboard_bp.route('/')
def index():
    """Landing page for visitors, dashboard for authenticated users."""
    if 'user_id' in session:
        user = get_current_user()
        uid = session['user_id']
        prefs = user_db.get_preferences(uid)
        # Dismiss the legacy onboarding modal and mark onboarding_version
        # so the feature tour (which gates on version >= 2) fires for net-new
        # users. tour_completed stays per-user so the tour runs once.
        if not prefs.get('onboarding_completed'):
            prefs['onboarding_completed'] = True
            prefs['onboarding_version'] = 2
            try:
                user_db.save_preferences(uid, prefs)
            except PreferencesWriteError as exc:
                # The page still renders; the flag is retried on the next visit.
                logger.error('onboarding flag not saved for user %s: %s', uid, exc)
        onboarding_completed = True
        onboarding_version = prefs.get('onboarding_version', 2)
        tour_completed = prefs.get('tour_completed', False)
        new_feedback_count = user_db.count_new_feedback() if session.get('is_admin') else 0
        # In-app AI chat is feature-flagged OFF for the MCP-first free beta;
        # hide its UI entirely when disabled. admins see it
        # regardless of the flag (one rule, ai_chat_allowed, shared with the
        # API gates). Never let a flag hiccup break the page.
        try:
            from carrac.integrations import ai_chat_allowed
            ai_chat_enabled = ai_chat_allowed(session.get('is_admin'))
        except Exception:
            ai_chat_enabled = False
        return render_template('index.html', user=user, onboarding_completed=onboarding_completed,
                               onboarding_version=onboarding_version, tour_completed=tour_completed,
                               new_feedback_count=new_feedback_count, ai_chat_enabled=ai_chat_enabled,
                               details_drawer_enabled=(os.environ.get('DETAILS_DRAWER_ENABLED') == '1'),
                               desk_enabled=(os.environ.get('DESK_ENABLED') == '1'))
    # Anonymous '/' renders the real landing page, not the coming-soon
    # holding page. Flip COMING_SOON_MODE=1 to revert (e.g. emergency takedown).
    if os.environ.get('COMING_SOON_MODE', '').lower() in ('1', 'true', 'yes'):
        return render_template('coming_soon.html')
    return render_template('landing_v5.html', report_teaser=_landing_report_teaser_v5())


@dashboard_bp.route('/desk')
def desk():
    """Trader's Desk — flag-gated unified workspace.

    DESK_ENABLED stays the GA switch: on, the Desk is open to everyone in that
    environment. Off, admins still get in.

    The 404 stays ahead of the login redirect on purpose: an anonymous probe
    must not learn the route exists.
    """
    if os.environ.get('DESK_ENABLED') != '1' and not session.get('is_admin'):
        abort(404)
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    return render_template('desk.html', user=get_current_user())


def _first_paragraph(markdown_text):
    """First substantive paragraph of a markdown report, plain-text-ish."""
    para = []
    for line in markdown_text.splitlines():
        line = line.strip()
        if not line:
            if para:
                break
            continue
        # Skip headings, rules, tables, html and metadata-looking lines
        if line.startswith(('#', '---', '|', '<', '>', '*Generated', '_Generated')):
            if para:
                break
            continue
        para.append(line)
    text = ' '.join(para)
    # Strip the most common markdown inline syntax
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = text.replace('`', '')
    return text.strip() if len(text.strip()) >= 60 else ''


def _report_bullets(markdown_text, limit=3):
    """First N list items from a markdown report, stripped to plain text."""
    out = []
    for line in (markdown_text or '').splitlines():
        s = line.strip()
        m = re.match(r'^([-*•]|\d+[.)])\s+(.*)', s)
        if not m:
            continue
        text = m.group(2).strip()
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
        text = text.replace('`', '').strip()
        if len(text) < 24:
            continue
        if len(text) > 180:
            text = text[:177].rsplit(' ', 1)[0] + '…'
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _landing_report_teaser_v5():
    """Richer teaser for the v5 landing report card: tldr + up to 3 bullets.

    GENERAL (operator-neutral) report only — never the operator's morning
    report (see carrac/reports sanitize). Degrades to None if unavailable;
    the template then shows a generic (non-fabricated) card.
    """
    try:
        from carrac.content_db import get_content_db
        report = get_content_db().get_latest_report(report_type='general')
        if not report:
            return None
        content = report.get('content') or ''
        tldr = (report.get('summary') or '').strip() or _first_paragraph(content)
        if not tldr:
            return None
        # the general report can lead with bullets — strip a leading list marker
        # so the TL;DR reads as a sentence, not "- ...".
        tldr = re.sub(r'^([-*•]|\d+[.)])\s+', '', tldr).strip()
        if len(tldr) > 320:
            tldr = tldr[:317].rsplit(' ', 1)[0] + '…'
        # bullets, minus any that duplicate the TL;DR line
        tnorm = tldr[:48].lower()
        bullets = [b for b in _report_bullets(content, limit=5) if b[:48].lower() != tnorm][:3]
        return {
            'date': (str(report.get('created_at') or ''))[:10],
            'tldr': tldr,
            'bullets': bullets,
        }
    except Exception as e:
        logger.debug(f"landing v5 teaser unavailable: {e}")
        return None


@dashboard_bp.route('/landingpage')
def landingpage():
    """Legacy preview URL → 301 to canonical '/' for cached links."""
    return redirect(url_for('dashboard.index'), code=301)


@dashboard_bp.route('/chart/<symbol>/<timeframe>')
@login_required
def chart_standalone(symbol: str, timeframe: str):
    """Standalone chart page with bookmarkable URL."""
    user = get_current_user()
    return render_template(
        'chart_standalone.html',
        symbol=symbol.upper(),
        timeframe=timeframe.upper(),
        user=user,
    )


@dashboard_bp.route('/indicators')
def indicators():
    """Public indicator guide page — no auth required."""
    return render_template('indicators.html')


@dashboard_bp.route('/changelog')
def changelog_page():
    """Version history page — PUBLIC: it is sitemap'd and robots.txt-allowed,
    so a login gate would send every anonymous visitor/crawler a 302->/login. It extends
    ``_marketing_base_v5.html`` (no session/current_user assumptions — same
    base as /guide, /mcp, /markets), so it renders anonymously with no
    further template changes.

    Passes the full changelog explicitly so all entries appear in the
    initial server-rendered HTML (not a JS skeleton).
    """
    return render_template('changelog.html', entries=CHANGELOG)


@dashboard_bp.route('/guide')
def guide():
    """Comprehensive user guide — no auth required."""
    return render_template('guide.html')


@dashboard_bp.route('/intelligence')
@dashboard_bp.route('/intelligence/<asset>')
def intelligence_page(asset=None):
    """Smart route — marketing page for anon/free/member, Intelligence app for Premium+.

    The app's old path /admin/intelligence still redirects here.
    Anchors from the marketing nav (/intelligence#regime etc.) still work for
    signed-out visitors; Premium+ users land in the app dashboard instead.

    Optional <asset> URL segment scopes the app to that asset's correlations,
    analogues, and lead-lag. Validated against TARGET_ASSETS; unknown assets
    redirect to the default (BTC) view.
    """
    tier = get_request_tier()
    if tier not in ('premium', 'tester', 'admin'):
        return render_template('intelligence.html')

    from quant.engine.config import TARGET_ASSETS
    available = sorted(TARGET_ASSETS.keys())
    asset_key = (asset or 'BTC').upper()
    if asset_key not in TARGET_ASSETS:
        return redirect(url_for('dashboard.intelligence_page'), code=302)
    return render_template(
        'intelligence_app.html',
        intel_asset=asset_key,
        available_assets=available,
    )


@dashboard_bp.route('/workflow')
def marketing_workflow():
    """Marketing page: Workflow — Discover / Frame / Execute / Learn."""
    return render_template('workflow.html')


@dashboard_bp.route('/integrations')
def marketing_integrations():
    """Marketing page: Integrations — MCP / Claude / Gemini / Cursor / Copilot."""
    return render_template('integrations.html')


@dashboard_bp.route('/markets')
def marketing_markets():
    """Marketing page: Markets — crypto, tradfi, macro, derivatives data."""
    return render_template('markets.html')


@dashboard_bp.route('/mcp')
def marketing_mcp():
    """Marketing page: MCP Server — bring-your-own-AI market data for AI agents."""
    return render_template('mcp.html')


@dashboard_bp.route('/market-regime')
def marketing_market_regime():
    """Marketing page: Market Regime Detection — live 2-state HMM, risk-on/risk-off context."""
    return render_template('market-regime.html')


def _assets_public():
    """True when the public /assets surface is flipped on (env ASSETS_PAGES_PUBLIC).
    Off by default → pages 404 for non-admins, carry noindex, absent from sitemap."""
    return bool(os.environ.get('ASSETS_PAGES_PUBLIC'))


@dashboard_bp.route('/assets')
@limiter.limit('60 per minute')
def assets_index():
    """Public asset directory — browse curated assets by cluster with a client-side
    filter only (no server search → no injection surface). Ships dark: 404 for
    non-admins + noindex until ASSETS_PAGES_PUBLIC is set."""
    public = _assets_public()
    if not public and not session.get('is_admin'):
        abort(404)
    from carrac.assets.public_pages import clustered_assets
    return render_template('assets_index.html',
                           groups=clustered_assets(),
                           assets_public=public,
                           canonical=site_base_url() + '/assets')


@dashboard_bp.route('/assets/<symbol>')
@limiter.limit('60 per minute')
def asset_detail(symbol):
    """Per-asset permalink. The symbol is WHITELISTED against the curated registry
    BEFORE any cache read (defends Redis/DB from arbitrary path input); unknown ->
    404. Read-only over the precomputed facts bundle (no recompute on the request
    path). Ships dark like the directory."""
    public = _assets_public()
    if not public and not session.get('is_admin'):
        abort(404)
    from carrac.assets.public_pages import validate_symbol
    sym = validate_symbol(symbol)
    if not sym:
        abort(404)
    from carrac.facts.wire import get_asset_facts
    from carrac.facts.labels import asset_name
    from carrac.facts.ladder import ladder_for
    try:
        b = get_asset_facts(sym)   # cache read; a miss/raise must not 500 the page
        if b:
            b["ladder"] = ladder_for(sym)   # rich spatial ladder (None when cache cold)
    except Exception:
        b = None
    return render_template('asset_detail.html',
                           symbol=sym, name=asset_name(sym),
                           facts_list=[b] if b else [],
                           assets_public=public,
                           canonical=site_base_url() + '/assets/' + sym)


@dashboard_bp.route('/design-system')
@tier_required('tester', 'admin')
def design_system():
    """Living UI style guide — source of truth for tokens, buttons, popups.

    Internal tool for designers/devs. Locked to admin + tester tiers; not
    visible to free/member/premium users.
    """
    return render_template('design-system.html')


@dashboard_bp.route('/api/health/hyperliquid-ws')
@login_required
def hl_ws_health():
    """Healthcheck for the Hyperliquid WebSocket live candle worker."""
    from data_sources.hyperliquid_ws import HyperliquidWSWorker
    return jsonify(HyperliquidWSWorker.read_health_state()), 200


@dashboard_bp.route('/api/health/ccxt-ws')
@login_required
def ccxt_ws_health():
    """Healthcheck for the CCXT WebSocket live candle worker."""
    from data_sources.ccxt_ws import CcxtWSWorker
    return jsonify(CcxtWSWorker.read_health_state()), 200


# ==================== Custom Formula Indicators ====================

_VALID_STYLES = {'line', 'histogram', 'dots', 'area'}
_VALID_PANES = {'overlay', 'new'}
_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def _validate_formula_data(data: dict) -> tuple:
    """Validate custom formula fields. Returns (cleaned_data, error_message)."""
    name = (data.get('name') or '').strip()
    if not name or len(name) > 30:
        return None, 'Name must be 1-30 characters'

    formula = (data.get('formula') or '').strip()
    if not formula or len(formula) > 2000:
        return None, 'Formula must be 1-2000 characters'

    style = data.get('style', 'line')
    if style not in _VALID_STYLES:
        return None, f'Style must be one of: {", ".join(_VALID_STYLES)}'

    color = data.get('color', '#2196F3')
    if not _COLOR_RE.match(color):
        return None, 'Color must be a hex color (e.g., #2196F3)'

    pane = data.get('pane', 'new')
    if pane not in _VALID_PANES:
        return None, 'Pane must be "overlay" or "new"'

    line_width = data.get('lineWidth', 1)
    try:
        line_width = int(line_width)
        if line_width < 1 or line_width > 3:
            line_width = 1
    except (TypeError, ValueError):
        line_width = 1

    # Strip HTML from name
    name = re.sub(r'<[^>]+>', '', name).strip()

    return {
        'id': data.get('id', ''),
        'name': name,
        'formula': formula,
        'style': style,
        'color': color,
        'pane': pane,
        'lineWidth': line_width
    }, None


@dashboard_bp.route('/api/custom-formulas', methods=['GET'])
@login_required
def get_custom_formulas():
    """Get all custom formulas for the current user."""
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    if not get_tier_limits(get_request_tier()).get('custom_formulas'):
        return jsonify({'formulas': []})
    user_id = session['user_id']
    formulas = user_db.get_custom_formulas(user_id)
    return jsonify({'formulas': formulas})


@dashboard_bp.route('/api/custom-formulas', methods=['POST'])
@login_required
def create_custom_formula():
    """Create a new custom formula."""
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    tier = get_request_tier()
    limits = get_tier_limits(tier)
    if not limits.get('custom_formulas'):
        return jsonify({'error': 'Custom formulas require Premium', 'upgrade_required': True}), 403

    user_id = session['user_id']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Missing data'}), 400

    cleaned, err = _validate_formula_data(data)
    if err:
        return jsonify({'error': err}), 400

    # Tier limit check
    current_count = user_db.count_custom_formulas(user_id)
    max_formulas = limits.get('max_custom_formulas', 3)
    if current_count >= max_formulas:
        return jsonify({
            'error': f'Custom formula limit reached ({current_count}/{max_formulas}). Upgrade for more.',
            'limit_type': 'custom_formula',
            'current': current_count,
            'max': max_formulas
        }), 403

    if not cleaned['id']:
        return jsonify({'error': 'Missing formula ID'}), 400

    success = user_db.save_custom_formula(user_id, cleaned)
    if success:
        return jsonify({'status': 'created', 'id': cleaned['id']}), 201
    return jsonify({'error': 'Failed to save formula'}), 500


@dashboard_bp.route('/api/custom-formulas/<formula_id>', methods=['PUT'])
@login_required
def update_custom_formula(formula_id):
    """Update an existing custom formula.

    This handler was tier-blind. `save_custom_formula` is an UPSERT,
    so a Free user could PUT arbitrary `formula_id` values and create
    unlimited Premium custom formulas. We now mirror the POST gate:
     (a) tier must allow custom_formulas,
     (b) if the formula_id isn't one the caller already owns, the
         max_custom_formulas limit must not be exceeded.
    """
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    tier = get_request_tier()
    limits = get_tier_limits(tier)
    if not limits.get('custom_formulas'):
        return jsonify({'error': 'Custom formulas require Premium',
                        'upgrade_required': True}), 403

    user_id = session['user_id']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Missing data'}), 400

    data['id'] = formula_id
    cleaned, err = _validate_formula_data(data)
    if err:
        return jsonify({'error': err}), 400

    # Determine whether this PUT is an update of a formula the caller already
    # owns, or a disguised create. Only the "owned by caller" case may skip
    # the count limit.
    existing_ids = {f['id'] for f in user_db.get_custom_formulas(user_id)}
    if formula_id not in existing_ids:
        current_count = len(existing_ids)
        max_formulas = limits.get('max_custom_formulas', 3)
        if current_count >= max_formulas:
            return jsonify({
                'error': f'Custom formula limit reached ({current_count}/{max_formulas}). Upgrade for more.',
                'limit_type': 'custom_formula',
                'current': current_count,
                'max': max_formulas,
            }), 403

    success = user_db.save_custom_formula(user_id, cleaned)
    if success:
        return jsonify({'status': 'updated'})
    return jsonify({'error': 'Failed to update formula'}), 500


@dashboard_bp.route('/api/custom-formulas/<formula_id>', methods=['DELETE'])
@login_required
def delete_custom_formula(formula_id):
    """Delete a custom formula."""
    user_id = session['user_id']
    success = user_db.delete_custom_formula(user_id, formula_id)
    if success:
        return jsonify({'status': 'deleted'}), 200
    return jsonify({'error': 'Formula not found'}), 404


@dashboard_bp.route('/api/strategies', methods=['GET'])
@login_required
def get_strategies():
    """Get all backtest strategies for the current user."""
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    if not get_tier_limits(get_request_tier()).get('backtester_save'):
        return jsonify({'strategies': []})  # Run-only mode — no saved strategies
    # Via the shared model layer, so this route and the MCP
    # backtest_manage_strategies tool cannot drift apart.
    from carrac.backtests.models import list_strategies
    user_id = session['user_id']
    return jsonify({'strategies': list_strategies(user_id)})


@dashboard_bp.route('/api/strategies', methods=['POST'])
@login_required
def create_strategy():
    """Create a new backtest strategy."""
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    if not get_tier_limits(get_request_tier()).get('backtester_save'):
        return jsonify({'error': 'Strategy save/load requires a subscription',
                        'upgrade_required': True}), 403
    user_id = session['user_id']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Missing data'}), 400

    name = (data.get('name') or '').strip()
    entry = (data.get('entry_formula') or '').strip()
    exit_f = (data.get('exit_formula') or '').strip()
    if not name or len(name) > 50:
        return jsonify({'error': 'Name must be 1-50 characters'}), 400
    if not entry or not exit_f:
        return jsonify({'error': 'Entry and exit formulas required'}), 400
    if len(entry) > 2000 or len(exit_f) > 2000:
        return jsonify({'error': 'Formula exceeds 2000 character limit'}), 400

    # Tier limit: same as custom formulas
    from carrac.backtests.models import count_strategies, save_strategy

    tier = get_request_tier()
    limits = get_tier_limits(tier)
    max_strats = limits.get('max_custom_formulas', 3)
    current = count_strategies(user_id)
    if current >= max_strats:
        return jsonify({'error': f'Strategy limit reached ({current}/{max_strats}). Upgrade for more.'}), 403

    # Everything the panel exposes beyond the seven legacy scalars.
    # Whitelisted rather than passed through, so a client cannot grow the stored
    # blob arbitrarily. Absent keys are simply not stored — a strategy saved
    # without a stop stays without one rather than gaining a zero.
    _PARAM_KEYS = (
        'timeframe', 'start_date', 'end_date', 'regime', 'instrument',
        'slippage_bps', 'stop_type', 'stop_loss_percent', 'take_profit_percent',
        'entry_mode',
    )
    params = {k: data[k] for k in _PARAM_KEYS if data.get(k) not in (None, '')}

    strategy_data = {
        'name': name,
        'entry_formula': entry,
        'exit_formula': exit_f,
        'direction': data.get('direction', 'long'),
        'capital': data.get('capital', 10000),
        'size_percent': data.get('size_percent', 100),
        'commission_percent': data.get('commission_percent', 0.1),
        'params': params or None,
    }

    # The model layer mints and returns the id — user_db.save_strategy
    # returned only a bool, so the id it generated was unreachable by any caller.
    new_id = save_strategy(user_id, strategy_data)
    if new_id:
        return jsonify({'status': 'created', 'id': new_id}), 201
    return jsonify({'error': 'Failed to save strategy'}), 500


@dashboard_bp.route('/api/strategies/<strategy_id>', methods=['DELETE'])
@login_required
def delete_strategy(strategy_id):
    """Delete a backtest strategy."""
    from carrac.backtests.models import delete_strategy as _delete
    user_id = session['user_id']
    if _delete(user_id, strategy_id):
        return jsonify({'status': 'deleted'}), 200
    return jsonify({'error': 'Strategy not found'}), 404


# ── Backtest run history ────────────────────────────────────────
#
# Saved strategy CONFIGURATIONS and saved RESULTS are separate: results live in
# backtest_runs, which MCP clients also write, so a backtest saved over MCP must
# be visible to the human on the same account. These two routes are
# the browser's door onto the same table backtest_get_runs reads.

@dashboard_bp.route('/api/backtest/runs', methods=['GET'])
@login_required
def get_backtest_runs():
    """List the current user's saved backtest runs, newest first."""
    from carrac.backtests.models import list_runs
    if not get_tier_limits(get_request_tier()).get('backtester_save'):
        return jsonify({'runs': []})  # Run-only mode — nothing is persisted
    runs = list_runs(
        session['user_id'],
        symbol=request.args.get('symbol') or None,
        limit=request.args.get('limit', 20),
        model=request.args.get('model') or None,
    )
    return jsonify({'runs': runs, 'count': len(runs)})


@dashboard_bp.route('/api/backtest/runs', methods=['POST'])
@login_required
def save_backtest_run():
    """Persist a backtest result the browser already ran and displayed.

    The panel's Run Backtest runs the Full Simulation engine
    entirely client-side (static/js/backtester.js) and this route must not
    repeat that work — it only stores the `result` the caller submits, the
    same numbers already reviewed on screen. That is the exact bug just fixed
    on the MCP side (backtest_save_run) — do not reintroduce it here.
    """
    from carrac.backtests.models import save_run, PERSISTABLE_MODELS
    from carrac.mcp.backtest_models import MODEL_FULL_SIMULATION

    if not get_tier_limits(get_request_tier()).get('backtester_save'):
        return jsonify({'error': 'Saving backtest runs requires a subscription',
                        'upgrade_required': True}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Missing data'}), 400

    symbol = (data.get('symbol') or '').upper().strip()
    expression = (data.get('expression') or '').strip()
    timeframe = (data.get('timeframe') or '').strip()
    result = data.get('result')
    # Never trust the client's `model` blindly — it selects which table row
    # shape is written and which summary fields get read back out.
    model = (data.get('model') or MODEL_FULL_SIMULATION).strip()

    if not symbol:
        return jsonify({'error': 'symbol is required'}), 400
    if not expression:
        return jsonify({'error': 'expression is required'}), 400
    if not timeframe:
        return jsonify({'error': 'timeframe is required'}), 400
    if not isinstance(result, dict) or not result:
        return jsonify({'error': 'result is required — pass the backtest you already ran, '
                        'this endpoint does not run one'}), 400
    if model not in PERSISTABLE_MODELS:
        return jsonify({'error': f'{model!r} cannot be saved as a run'}), 400

    name = (data.get('name') or '').strip() or None
    if name and len(name) > 255:
        return jsonify({'error': 'Name exceeds 255 characters'}), 400

    try:
        lookback_bars = int(data.get('lookback_bars') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'lookback_bars must be a number'}), 400

    # Same field vocabulary carrac/mcp/tools/backtest.py _persist_simulation
    # uses for its config blob, so a run saved here and one saved via MCP are
    # indistinguishable downstream — this is relabeling the panel's own field
    # names, not deriving or computing anything.
    raw_config = data.get('config') or {}
    if not isinstance(raw_config, dict):
        raw_config = {}
    config = {
        'entry_expression': expression,
        'exit_expression': raw_config.get('exit'),
        'direction': raw_config.get('direction'),
        'timeframe': timeframe,
        'instrument': raw_config.get('instrument'),
        'stop_type': raw_config.get('stopType'),
        'stop_percent': raw_config.get('stopLossPercent'),
        'target_percent': raw_config.get('takeProfitPercent'),
        'entry_mode': raw_config.get('entryMode'),
        'size_percent': raw_config.get('sizePercent'),
        'commission_percent': raw_config.get('commissionPercent'),
        'slippage_bps': raw_config.get('slippageBps'),
        'initial_capital': raw_config.get('initialCapital'),
    }
    config = {k: v for k, v in config.items() if v not in (None, '')}

    # Lift the list-view summary field out of the submitted stats — the same
    # alias _persist_simulation applies (result['trades'] = trade_count).
    # Aliasing an existing key is not a computation.
    stored_result = dict(result)
    if 'trades' not in stored_result and 'totalTrades' in stored_result:
        stored_result['trades'] = stored_result.get('totalTrades')

    try:
        run_id = save_run(
            session['user_id'],
            symbol=symbol[:50],
            expression=expression,
            timeframe=timeframe[:10],
            lookback_bars=lookback_bars,
            hold_bars=None,
            result=stored_result,
            name=name,
            model=model,
            config=config or None,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    return jsonify({'status': 'created', 'run_id': run_id}), 201


@dashboard_bp.route('/api/backtest/runs/<int:run_id>', methods=['GET'])
@login_required
def get_backtest_run_detail(run_id):
    """One saved run in full, including the config that produced it."""
    from carrac.backtests.models import get_run
    if not get_tier_limits(get_request_tier()).get('backtester_save'):
        return jsonify({'error': 'Saved runs require a subscription'}), 403
    run = get_run(session['user_id'], run_id)
    if run is None:
        return jsonify({'error': 'Run not found'}), 404
    return jsonify(run)


@dashboard_bp.route('/api/backtest/runs/<int:run_id>', methods=['DELETE'])
@login_required
def delete_backtest_run(run_id):
    """Delete one saved run."""
    from carrac.backtests.models import delete_run
    if delete_run(session['user_id'], run_id):
        return jsonify({'status': 'deleted'}), 200
    return jsonify({'error': 'Run not found'}), 404


@dashboard_bp.route('/api/tier-info')
@login_required
def tier_info():
    """Return current user's tier limits for client-side gating."""
    tier = get_request_tier()
    limits = get_tier_limits(tier)
    # Return a safe subset (no internal keys like blocked_tfs frozenset)
    return jsonify({
        'tier': tier,
        'max_chart_panes': limits.get('max_chart_panes', 2),
        'ai_queries_monthly': limits.get('ai_queries_monthly', 0),
        'cross_device_sync': limits.get('cross_device_sync', False),
        'context_composer': limits.get('context_composer', False),
        'backtester_save': limits.get('backtester_save', False),
        'data_export': limits.get('data_export', False),
        'derivatives': limits.get('derivatives', False),
        'max_scenarios': limits.get('max_scenarios', 0),
        'max_custom_formulas': limits.get('max_custom_formulas', 3),
        # The settings plan card renders these — serve the real
        # enforcement numbers so displayed caps can't drift from TIER_LIMITS.
        'max_plays': limits.get('max_plays', 0),
        'max_alerts': limits.get('max_alerts', 0),
        'telegram': limits.get('telegram', False),
        'discord': limits.get('discord', False),
        'topups_allowed': limits.get('topups_allowed', False),
        'weekly_review': limits.get('weekly_review', False),
        'tts': limits.get('tts', False),
        # Paid-tier launch matrix (webhooks consumed by the
        # settings-page card).
        'journal_trades_monthly': limits.get('journal_trades_monthly', 0),
        'webhooks': limits.get('webhooks', False),
        'journal_analytics': limits.get('journal_analytics', False),
        # Exchange auto-sync card lock (settings.html). Closed
        # default — 'journal_autosync' is intentionally absent from
        # TIER_LIMITS until a parallel branch wires it into the paid-tier
        # matrix, so this reads False for every tier until then.
        'journal_autosync': limits.get('journal_autosync', False),
    })


@dashboard_bp.route('/formulas')
def formula_reference():
    """Formula reference documentation page (public, no auth required)."""
    return render_template('formula-reference.html')


# ==================== Screening Matrix & Asset Cockpit ====================

@dashboard_bp.route('/api/details/matrix')
@auth_or_key_required
def get_matrix():
    """Screening Matrix — 10 indicator columns for all watchlist assets.

    Query params:
      sort=<column>  (default: symbol)
      dir=asc|desc   (default: asc)
      filter=<category>  (crypto, stock, etf, forex, index)
    """
    from carrac.precompute import response_cache
    from carrac.indicator_state import get_matrix_data
    from user_db import user_db

    rows = get_matrix_data(response_cache)
    if not rows:
        return jsonify({'rows': [], 'count': 0})

    # Filter to user watchlist if available. Resolve the per-user API-key owner
    # first (g.api_key_user_id) so headless clients (the Carrac TUI) get THEIR
    # watchlist-filtered matrix instead of the full universe; fall back to session.
    user_id = getattr(g, 'api_key_user_id', None)
    if user_id is None:
        user_id = session.get('user_id')
    watchlist = set(user_db.get_flat_watchlist(user_id)) if user_id else None

    # Optional category filter
    cat_filter = request.args.get('filter', '').lower()
    if cat_filter:
        rows = [r for r in rows if r.get('type', '').lower() == cat_filter]

    # Tab filter: watchlist only by default, 'all' for everything
    tab = request.args.get('tab', 'watchlist')
    if tab == 'watchlist' and watchlist:
        rows = [r for r in rows if r['symbol'] in watchlist]

    # Sort
    sort_col = request.args.get('sort', 'symbol')
    sort_dir = request.args.get('dir', 'asc')

    sort_keys = {
        'symbol': lambda r: r.get('symbol', ''),
        'price': lambda r: r.get('price') or 0,
        'change': lambda r: r.get('change_pct') or 0,
        'ema': lambda r: {'bullish': 2, 'neutral': 1, 'bearish': 0}.get(
            (r.get('ema_ribbon') or {}).get('trend', 'neutral'), 1),
        'rsi': lambda r: (r.get('rsi') or {}).get('value') or 50,
        'bb': lambda r: (r.get('bb_squeeze') or {}).get('width_pct') or 0,
        'consensio': lambda r: (r.get('consensio') or {}).get('signal') or 0,
        'structure': lambda r: {'uptrend': 2, 'downtrend': 0}.get(
            (r.get('structure') or {}).get('trend', ''), 1),
        'macd': lambda r: (r.get('macd') or {}).get('histogram') or 0,
        'adx': lambda r: (r.get('adx') or {}).get('value') or 0,
        'cpr': lambda r: {'above': 2, 'inside': 1, 'below': 0}.get(
            (r.get('cpr') or {}).get('position', ''), 1),
        'atr': lambda r: (r.get('atr_pct') or {}).get('value') or 0,
        'volume': lambda r: (r.get('rel_volume') or {}).get('ratio') or 0,
    }

    key_fn = sort_keys.get(sort_col, sort_keys['symbol'])
    rows.sort(key=key_fn, reverse=(sort_dir == 'desc'))

    # Free tier: Matrix is a synthesis view (indicator-alignment heatmap) — preview
    # the first N rows and flag the response so the client shows an upsell card.
    total = len(rows)
    tier = get_request_tier()
    preview_mode = (tier == 'free')
    FREE_MATRIX_ROW_LIMIT = 5
    if preview_mode:
        rows = rows[:FREE_MATRIX_ROW_LIMIT]

    payload = {
        'rows': rows,
        'count': len(rows),
        'sort': sort_col,
        'dir': sort_dir,
    }
    if preview_mode:
        payload['preview_mode'] = True
        payload['preview_tier'] = 'member'
        payload['total_available'] = total
        payload['preview_reason'] = 'Upgrade to Member to see the full indicator-alignment matrix and drill into any asset.'
    return jsonify(payload)


# ── Satisfaction Survey ─────────────────────────────────────────────

@dashboard_bp.route('/api/satisfaction/should-show')
@login_required
def satisfaction_should_show():
    """Check if the monthly satisfaction survey should be shown."""
    show = user_db.should_show_satisfaction_survey(session['user_id'])
    return jsonify({'show': show})


@dashboard_bp.route('/api/satisfaction', methods=['POST'])
@login_required
def submit_satisfaction():
    """Submit a satisfaction survey response."""
    data = request.get_json(silent=True) or {}
    rating = data.get('rating')
    comment = (data.get('comment') or '').strip()[:2000]

    if not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({'error': 'Rating must be 1-5'}), 400

    uid = session['user_id']
    rid = user_db.create_satisfaction_rating(uid, rating, comment)
    if not rid:
        return jsonify({'error': 'Failed to save'}), 500

    # Mark as prompted so it won't show again for 30 days
    user_db.set_satisfaction_prompted(uid)

    # Fire integrations (PostHog + EspoCRM)
    from carrac.integrations import on_satisfaction_rating
    on_satisfaction_rating(uid, session.get('email', ''), rating, comment or None)

    logger.info(f"Satisfaction rating {rating}/5 from user {uid}")
    return jsonify({'ok': True, 'id': rid})


@dashboard_bp.route('/api/satisfaction/dismiss', methods=['POST'])
@login_required
def dismiss_satisfaction():
    """Dismiss the satisfaction survey (counts as prompted for the month)."""
    uid = session['user_id']
    user_db.set_satisfaction_prompted(uid)

    # Track dismissal in PostHog
    from carrac.integrations import _posthog_capture, _fire_and_forget
    _fire_and_forget(_posthog_capture, str(uid), 'satisfaction_survey_dismissed')

    return jsonify({'ok': True})
