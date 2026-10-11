"""
Authentication decorators — shared across all blueprints.
"""

import hmac
import logging
import os
import threading
import time
from functools import wraps

from flask import current_app, g, session, request, redirect, url_for, jsonify, flash

logger = logging.getLogger('carrac.auth')

# ── Dynamic tier config cache ──
_tier_config_cache = {}    # {tier: {overrides from DB}}
_tier_config_ts = 0.0
_tier_config_lock = threading.Lock()
_TIER_CACHE_TTL = 60       # seconds

# ── Per-user tier override cache ──
# get_tier_limits() calls user_db.get_user_overrides(uid) for the session
# user on EVERY authed request — an uncached Postgres round-trip (plus a
# commit) on the hottest path in the app, including chart cache hits. Cache
# BOTH found and not-found (None) results here: most users have no overrides
# so the None case is the common one worth caching. Write paths
# (user_db.set_user_overrides/clear_user_overrides) call
# invalidate_user_overrides_cache() directly after a successful write; the
# 60s TTL below is the safety net for any missed invalidation path AND for
# cross-process staleness (4 gunicorn workers, each with its own in-process
# cache — a direct invalidation only clears the writing worker's copy).
_user_overrides_cache = {}   # {user_id: (ts, overrides_dict_or_None)}
_user_overrides_lock = threading.Lock()
_USER_OVERRIDES_TTL = 60     # seconds
_USER_OVERRIDES_MAX = 500    # bounded — evict stale entries opportunistically

# ── Tier-based feature limits ──
# Tiers: free (no AI), member ($34.99), tester (admin-granted, mirrors member),
#         premium ($69.00), admin (full access)
# Empty on purpose: "ALL charting free" (free-TradingView-alternative
# positioning; the landing page promises every intraday timeframe free).
# Re-gate instantly via /admin/tiers DB override if load demands it.
FREE_BLOCKED_TFS = frozenset()

_PAID_LIMITS = {
    # Premium-tier baseline. Member overrides max_alerts and pattern_match_daily below
    # to reinforce the tier-climb value story (Premium = power-user headroom).
    'max_alerts': 200,
    'derivatives': True,
    'telegram': True,
    'discord': True,
    'auto_monitoring': True,
    'blocked_tfs': frozenset(),
    'topups_allowed': True,
    'cross_device_sync': True,
    'context_composer': True,
    'backtester_save': True,
    'pattern_match_daily': 99,
    'data_export': True,
    'weekly_digest': True,
    'hyperliquid_enabled': True,  # kill switch — flip False to disable HL fallback
    'journal_autosync': True,  # exchange auto-sync (paid feature)
    # Paid-tier launch: shared across member/tester/premium/admin —
    # none of those tiers override these below, so every paid tier gets the
    # same launch-day matrix without duplicating the keys per tier.
    'journal_trades_monthly': 999,  # unlimited sentinel — matches the max_plays/
                                     # max_scenarios/max_custom_formulas convention
    'webhooks': True,
    'journal_analytics': True,
}

TIER_LIMITS = {
    'free': {
        'max_plays': 3,
        'max_scenarios': 0,
        'max_milestones': 0,
        'max_alerts': 5,
        'max_custom_formulas': 3,
        'derivatives': False,
        'journal_autosync': False,
        'personal_report': 'none',
        'telegram': False,
        'discord': False,
        'auto_monitoring': False,
        'blocked_tfs': FREE_BLOCKED_TFS,
        'ai_queries_monthly': 0,
        'ai_token_budget': 4000,
        'topups_allowed': False,
        'cross_device_sync': False,
        'context_composer': False,
        'custom_recipes': False,
        'backtester_save': False,
        'pattern_match_daily': 0,
        'data_export': False,
        'max_chart_panes': 4,
        'tts': False,
        'weekly_digest': False,
        'weekly_review': False,
        'quant_engine': False,
        'custom_formulas': False,
        'backtester': False,
        'hyperliquid_enabled': True,  # HL coins available to all tiers (coverage, not paywall)
        # Paid-tier launch matrix.
        'journal_trades_monthly': 20,
        'webhooks': False,
        'journal_analytics': False,
    },
    'member': {
        **_PAID_LIMITS,
        'max_plays': 999,
        'max_scenarios': 15,
        'max_milestones': 6,
        'max_alerts': 100,
        'max_custom_formulas': 999,
        'personal_report': 'standard',
        'ai_queries_monthly': 40,
        'ai_token_budget': 8000,
        'custom_recipes': False,
        'max_chart_panes': 4,
        'pattern_match_daily': 20,
        'tts': False,
        'weekly_review': False,
        # The entry paid tier must include the
        # product's actual differentiators — quant read + backtesting —
        # or it converts on nothing (custom formulas stay premium).
        'quant_engine': True,
        'custom_formulas': False,
        'backtester': True,
    },
    'tester': {
        **_PAID_LIMITS,
        # Tester is "admin-granted, mirrors member" — keep max_plays/
        # max_alerts equal to member's launch-matrix values rather than stranding
        # testers below paying members after the member bump to 999/100.
        'max_plays': 999,
        'max_scenarios': 15,
        'max_milestones': 6,
        'max_alerts': 100,
        'max_custom_formulas': 999,
        'personal_report': 'standard',
        'ai_queries_monthly': 40,
        'ai_token_budget': 8000,
        'custom_recipes': False,
        'max_chart_panes': 4,
        'pattern_match_daily': 20,
        'tts': False,
        'weekly_review': False,
        'quant_engine': True,
        'custom_formulas': True,
        'backtester': True,
    },
    'premium': {
        **_PAID_LIMITS,
        'max_plays': 999,
        'max_scenarios': 999,
        'max_milestones': 10,
        'max_custom_formulas': 999,
        'personal_report': 'full',
        'ai_queries_monthly': 150,
        'ai_token_budget': 12000,
        'custom_recipes': True,
        'max_chart_panes': 4,
        'tts': True,
        'weekly_review': True,
        'quant_engine': True,
        'custom_formulas': True,
        'backtester': True,
    },
    'admin': {
        **_PAID_LIMITS,
        'max_plays': 999,
        'max_scenarios': 999,
        'max_milestones': 10,
        'max_custom_formulas': 999,
        'personal_report': 'full',
        'ai_queries_monthly': 999,
        'ai_token_budget': 20000,
        'custom_recipes': True,
        'max_chart_panes': 4,
        'tts': True,
        'weekly_review': True,
        'quant_engine': True,
        'custom_formulas': True,
        'backtester': True,
    },
}
PREMIUM_DERIVATIVES = frozenset({'oi', 'cvd_usdt', 'cvd_coin'})
from carrac.tiers import PAID_TIERS  # noqa: E402,F401 — after TIER_LIMITS to avoid circular imports; re-exported for callers


def _refresh_tier_config_cache():
    """Refresh the tier config cache from DB if stale."""
    global _tier_config_cache, _tier_config_ts
    now = time.time()
    if now - _tier_config_ts < _TIER_CACHE_TTL:
        return
    with _tier_config_lock:
        # Double-check after acquiring lock
        if now - _tier_config_ts < _TIER_CACHE_TTL:
            return
        try:
            from user_db import user_db
            _tier_config_cache = user_db.get_all_tier_configs()
            _tier_config_ts = now
        except Exception as e:
            logger.debug(f"Tier config cache refresh failed (using defaults): {e}")
            _tier_config_ts = now  # Don't retry immediately on error


def get_tier_limits(tier='free'):
    """Return limits dict for a given tier.

    Merges: hardcoded defaults <- DB tier_config overrides <- session per-user overrides.
    Per-user overrides are applied automatically when a session user_id exists.
    """
    _refresh_tier_config_cache()
    base = _get_tier_base(tier)
    # Apply per-user overrides if session available
    try:
        uid = session.get('user_id')
        if uid:
            user_ov = _get_cached_user_overrides(uid)
            if user_ov:
                user_ov = dict(user_ov)  # never mutate the cached object
                user_ov.pop('_reason', None)
                base.update(user_ov)
    except Exception:
        pass
    _normalize_blocked_tfs(base)
    return base


def _get_tier_base(tier='free'):
    """Return hardcoded + DB config limits (no per-user overrides)."""
    base = dict(TIER_LIMITS.get(tier, TIER_LIMITS['free']))
    db_overrides = _tier_config_cache.get(tier)
    if db_overrides:
        base.update(db_overrides)
    return base


def _normalize_blocked_tfs(limits):
    """Normalize blocked_tfs to frozenset (DB/JSON roundtrip produces list)."""
    bt = limits.get('blocked_tfs')
    if bt is not None and not isinstance(bt, frozenset):
        limits['blocked_tfs'] = frozenset(bt) if bt else frozenset()


def get_tier_limits_for_user(user_id, tier='free'):
    """Return limits dict with per-user overrides applied.

    Merges: hardcoded defaults <- DB tier_config <- user_tier_overrides.
    Does NOT apply session user's overrides — only the specified user_id.
    """
    _refresh_tier_config_cache()
    limits = _get_tier_base(tier)
    try:
        overrides = _get_cached_user_overrides(user_id)
        if overrides:
            overrides = dict(overrides)  # never mutate the cached object
            overrides.pop('_reason', None)
            limits.update(overrides)
    except Exception:
        pass
    _normalize_blocked_tfs(limits)
    return limits


def invalidate_tier_config_cache():
    """Force the next get_tier_limits() call to re-read from DB."""
    global _tier_config_ts
    _tier_config_ts = 0.0


def _get_cached_user_overrides(user_id):
    """Return per-user tier overrides for ``user_id``, cached ~60s.

    Caches both hits and misses (None). Only a fresh cache entry short-
    circuits the DB — a miss (not yet cached, or expired) always falls
    through to ``user_db.get_user_overrides``.
    """
    now = time.time()
    with _user_overrides_lock:
        entry = _user_overrides_cache.get(user_id)
        if entry and (now - entry[0]) < _USER_OVERRIDES_TTL:
            return entry[1]
    from user_db import user_db
    overrides = user_db.get_user_overrides(user_id)
    with _user_overrides_lock:
        _user_overrides_cache[user_id] = (now, overrides)
        if len(_user_overrides_cache) > _USER_OVERRIDES_MAX:
            stale = [uid for uid, (ts, _) in _user_overrides_cache.items()
                     if now - ts > _USER_OVERRIDES_TTL]
            for uid in stale:
                del _user_overrides_cache[uid]
    return overrides


def invalidate_user_overrides_cache(user_id):
    """Force the next per-user override lookup for ``user_id`` to re-query the DB.

    Called by ``user_db.set_user_overrides``/``clear_user_overrides`` right
    after a successful write. Only clears the calling worker's own cache —
    see the TTL note on ``_user_overrides_cache`` above for the cross-process
    case.
    """
    with _user_overrides_lock:
        _user_overrides_cache.pop(user_id, None)


def get_request_tier():
    """Tier for current request. Pool-aware, falls back to legacy check."""
    if session.get('user_id'):
        return session.get('tier', 'free')
    # Pool-based tier (set by rate_pools.classify_request)
    if hasattr(g, 'rate_tier'):
        return g.rate_tier
    # Legacy fallback for code paths that skip before_request.
    # Header-only: URL-param keys removed so the
    # tier lookup cannot be poisoned via ?key= and ends up in access logs.
    # Accepts X-Internal-Key (server-to-server) or X-API-Key (external).
    key = _extract_api_key_from_request()
    if key and verify_api_key(key):
        # A per-user key carries its owner's tier; only the shared internal
        # key (no user id) is the admin tier.
        if getattr(g, 'api_key_user_id', None) is not None:
            return getattr(g, 'api_key_tier', None) or 'free'
        return 'admin'
    return 'free'


def verify_api_key(provided_key: str) -> bool:
    """Verify API key: shared admin key OR per-user key (cvx_k_ prefix).

    For per-user keys, sets on flask.g:
      - api_key_user_id: int
      - api_key_tier: str (inherited from user's account)
      - api_key_scope: str ('full' or 'read-only')

    The shared key is honoured ONLY when the request's
    ``X-Internal-Key`` header itself carries it — on the public, documented
    ``X-API-Key`` header (or a URL param) it is just an unknown string. The
    check reads the header directly rather than trusting whatever string the
    caller extracted, so the dozen call sites that read only ``X-API-Key``
    need no changes to be correct: their shared-key branch simply went dead,
    which is the point. Outside a request context the shared path fails
    closed (some management/test code calls this under app context only).
    """
    if not provided_key:
        return False
    # Path 1: shared admin key — internal callers (engine, crons, self-calls)
    # only, and only via X-Internal-Key.
    expected = os.getenv('SCREENSHOT_API_KEY', '')
    if expected and hmac.compare_digest(provided_key, expected):
        return _shared_key_presented_internally(expected)
    # Path 2: per-user API key (cvx_k_ prefix)
    if provided_key.startswith('cvx_k_'):
        from carrac.apikeys.auth import hash_api_key, validate_key_format
        if not validate_key_format(provided_key):
            return False
        from user_db import user_db
        key_info = user_db.verify_api_key_token(hash_api_key(provided_key))
        if key_info:
            g.api_key_user_id = key_info['user_id']
            g.api_key_tier = key_info['tier']
            g.api_key_scope = key_info.get('scope', 'full')
            return True
    return False


def _shared_key_presented_internally(expected: str) -> bool:
    """True when the current request's ``X-Internal-Key`` header carries the
    shared key. Fails closed outside a request context."""
    from flask import has_request_context
    if not has_request_context():
        return False
    return hmac.compare_digest(
        request.headers.get('X-Internal-Key', ''), expected)


def resolve_api_key(
    provided_key: str, *, allow_shared: bool = False,
) -> tuple[bool, int | None, str | None, str | None, bool]:
    """Runtime-agnostic API-key verification.

    Returns ``(ok, user_id, tier, scope, is_internal)`` without touching
    ``flask.g``. Used by the FastAPI MCP router so the same auth logic runs
    in both runtimes.

    For the shared admin key (``SCREENSHOT_API_KEY``), returns
    ``(True, None, None, None, False)`` — the caller is expected to default
    the tier to ``'admin'`` (invariant: never default user_id to 1).

    ``is_internal``: True only for a per-user key
    explicitly flagged in the DB — excludes that key from tier metering /
    usage analytics / the weekly usage review's LLM-spend attribution. False for every
    other key, including the shared admin key.

    ``allow_shared`` — the caller's declaration that the key
    arrived via ``X-Internal-Key``. This function is framework-neutral and
    cannot see headers; the extractor that read the headers must compute it.
    Defaults to the safe side; per-user keys ignore it.
    """
    if not provided_key:
        return (False, None, None, None, False)
    expected = os.getenv('SCREENSHOT_API_KEY', '')
    if allow_shared and expected and hmac.compare_digest(provided_key, expected):
        return (True, None, None, None, False)
    if provided_key.startswith('cvx_k_'):
        from carrac.apikeys.auth import hash_api_key, validate_key_format
        if not validate_key_format(provided_key):
            return (False, None, None, None, False)
        from user_db import user_db
        key_info = user_db.verify_api_key_token(hash_api_key(provided_key))
        if key_info:
            return (
                True,
                key_info['user_id'],
                key_info['tier'],
                key_info.get('scope', 'full'),
                bool(key_info.get('is_internal', False)),
            )
    return (False, None, None, None, False)


def verify_admin_key(provided_key: str) -> bool:
    """True ONLY for the shared admin key (``SCREENSHOT_API_KEY``).

    Unlike :func:`verify_api_key`, a per-user ``cvx_k_`` key is rejected.  Use
    this on server-to-server "engine"/admin endpoints that act across users
    (read another user's data, write shared report content, debit any user's
    credits).  Those endpoints must not treat a self-minted per-user key as
    trusted engine auth (any logged-in user, incl. free tier, can mint one via
    ``POST /api/keys``).  Mirrors the ``resolve_api_key`` shared-key path
    (``user_id is None``) without touching ``flask.g``.

    Same header rule as ``verify_api_key`` — these are the most
    sensitive routes (cross-user credits, report writes), so the shared key
    must arrive via ``X-Internal-Key`` here too, or the split would leave
    the exact hole open on the engine surface it matters most.
    """
    if not provided_key:
        return False
    expected = os.getenv('SCREENSHOT_API_KEY', '')
    if not (expected and hmac.compare_digest(provided_key, expected)):
        return False
    return _shared_key_presented_internally(expected)


def login_required(f):
    """Require session login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def enforce_session_csrf():
    """Enforce CSRF for session-authenticated mutating requests.

    Registered as a ``before_request`` hook on the journal and dashboard
    blueprints. API-key callers and safe methods pass through.
    """
    if not current_app.config.get('WTF_CSRF_ENABLED', True):
        return None
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if api_key and verify_api_key(api_key):
        return None
    if session.get('user_id'):
        from flask_wtf.csrf import validate_csrf
        try:
            csrf_token = request.headers.get('X-CSRFToken') or request.form.get('csrf_token')
            validate_csrf(csrf_token)
        except Exception:
            return jsonify({'error': 'CSRF token missing or invalid'}), 403
    return None


def _extract_api_key_from_request() -> str:
    """Extract API key from request headers.

    Checks ``X-Internal-Key`` first (preferred for server-to-server callers),
    then ``X-API-Key`` (used by external/SDK callers).  Returns the first
    non-empty value, or an empty string if neither header is present.
    """
    return (
        request.headers.get('X-Internal-Key', '')
        or request.headers.get('X-API-Key', '')
    )


def auth_or_key_required(f):
    """Allow access via session login OR API key.

    Accepts key via ``X-Internal-Key`` or ``X-API-Key`` header.
    ``?key=`` URL param is explicitly rejected with 400.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' in session:
            return f(*args, **kwargs)
        if request.args.get('key'):
            logger.warning("[SECURITY] Rejected: API key passed as URL parameter on %s. Use X-Internal-Key or X-API-Key header.", request.path)
            return jsonify({'error': 'API key via URL parameter is deprecated. Use X-Internal-Key or X-API-Key header.'}), 400
        api_key = _extract_api_key_from_request()
        if verify_api_key(api_key):
            return f(*args, **kwargs)
        if request.is_json or request.accept_mimetypes.best == 'application/json':
            return jsonify({'error': 'Authentication required'}), 401
        return redirect(url_for('auth.login'))
    return decorated


def tester_or_key_required(f):
    """Allow access via API key OR session with tester/premium/admin tier."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # API key access — check key first, then enforce tier for per-user keys
        api_key = _extract_api_key_from_request()
        if verify_api_key(api_key):
            # Shared admin key (e.g. SCREENSHOT_API_KEY): verify_api_key returns True
            # but does NOT set g.api_key_tier — always allow (internal/bot callers)
            key_tier = getattr(g, 'api_key_tier', None)
            if key_tier is None:
                return f(*args, **kwargs)  # admin/internal key
            if key_tier in ('tester', 'premium', 'admin'):
                return f(*args, **kwargs)
            return jsonify({'error': 'Premium access required', 'upgrade_required': True}), 403
        # Session access — require tester, premium, or admin tier
        if 'user_id' in session:
            tier = session.get('tier', 'free')
            if tier in ('tester', 'premium', 'admin'):
                return f(*args, **kwargs)
            return jsonify({'error': 'Premium access required', 'upgrade_required': True}), 403
        if request.is_json or request.accept_mimetypes.best == 'application/json':
            return jsonify({'error': 'Authentication required'}), 401
        return redirect(url_for('auth.login'))
    return decorated


_FEATURE_LABELS = {
    'quant_engine': 'Quant Engine',
    'backtester': 'Backtester',
    'backtester_save': 'Saved backtests',
}


def tier_feature_required(feature):
    """Factory: allow access via API key OR session, gated on a TIER_LIMITS
    feature knob (e.g. 'quant_engine', 'backtester') instead of a hardcoded
    tier allowlist.

    ``tester_or_key_required`` hardcodes the allowlist to
    ``('tester', 'premium', 'admin')`` — a tier restructure that grants a
    feature to 'member' (as is done for quant_engine/backtester) has
    no effect here until every call site is found and edited by hand. This
    decorator instead reads ``get_tier_limits(tier).get(feature)`` at
    request time, so it automatically reflects TIER_LIMITS (including live
    DB tier_config overrides) — the tier restructure that broke access is
    itself the fix, with no further code changes required downstream.

    Preserves ``tester_or_key_required``'s API-key semantics: the shared
    admin key (``SCREENSHOT_API_KEY``) always passes; a per-user ``cvx_k_``
    key is tier-resolved from the key's *owning user* (via
    ``get_tier_limits_for_user``, not the caller's own session) so key-based
    access matches exactly what that user would see logged in.

    NOTE: detecting the shared admin key by ``g.api_key_user_id`` being unset
    (NOT by ``g.api_key_tier`` being unset, unlike a naive reading of
    ``tester_or_key_required``'s own comment). ``rate_pools.classify_request``
    runs as a ``before_request`` hook ahead of this decorator on every real
    request and already stamps ``g.api_key_tier = 'admin'`` for the shared
    key (only ``g.api_key_user_id`` stays unset for it) — so by the time this
    decorator's own ``verify_api_key()`` call runs, ``g.api_key_tier`` is
    already non-None for the shared key too.
    """
    label = _FEATURE_LABELS.get(feature, feature)

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            # API key access — check key first, then enforce the feature knob
            # for per-user keys (shared admin key always passes, as before).
            api_key = _extract_api_key_from_request()
            if verify_api_key(api_key):
                key_user_id = getattr(g, 'api_key_user_id', None)
                if key_user_id is None:
                    return f(*args, **kwargs)  # shared admin/internal key
                key_tier = getattr(g, 'api_key_tier', None) or 'free'
                limits = get_tier_limits_for_user(key_user_id, key_tier)
                if limits.get(feature, False):
                    return f(*args, **kwargs)
                return jsonify({'error': f'{label} requires a paid subscription',
                                'upgrade_required': True}), 403
            # Session access — gate on the session tier's feature knob.
            if 'user_id' in session:
                tier = session.get('tier', 'free')
                limits = get_tier_limits(tier)
                if limits.get(feature, False):
                    return f(*args, **kwargs)
                return jsonify({'error': f'{label} requires a paid subscription',
                                'upgrade_required': True}), 403
            if request.is_json or request.accept_mimetypes.best == 'application/json':
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('auth.login'))
        return decorated
    return decorator


def admin_required(f):
    """Require admin session with DB re-verification."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        if not session.get('is_admin'):
            return jsonify({'error': 'Admin access required'}), 403
        # Re-verify admin status from DB (don't trust session alone)
        try:
            from user_db import user_db
            user = user_db.get_user_by_id(session['user_id'])
            if not user or not user.get('is_admin') or not user.get('is_active', True):
                session.clear()
                return redirect(url_for('auth.login'))
        except Exception as e:
            logger.warning(f"[SECURITY] Admin DB re-verification failed — denying access: {e}")
            # Don't nuke the session on transient DB errors — just deny the admin page
            flash('Admin panel temporarily unavailable — please try again.', 'warning')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


def tier_required(*allowed_tiers):
    """Factory returning a decorator that checks session tier against allowed tiers."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                if request.is_json or request.accept_mimetypes.best == 'application/json':
                    return jsonify({'error': 'Authentication required'}), 401
                return redirect(url_for('auth.login'))
            tier = session.get('tier', 'free')
            if tier in allowed_tiers:
                return f(*args, **kwargs)
            if request.is_json or request.accept_mimetypes.best == 'application/json':
                return jsonify({'error': 'Subscription required', 'upgrade_required': True,
                                'current_tier': tier,
                                'required_tiers': list(allowed_tiers)}), 403
            return redirect('/')
        return decorated
    return decorator


def premium_required(f):
    """Require any paid tier (member, tester, premium, admin)."""
    return tier_required('member', 'tester', 'premium', 'admin')(f)


def get_current_user():
    """Get current logged-in user info from session."""
    if 'user_id' not in session:
        return None
    return {
        'id': session.get('user_id'),
        'email': session.get('email'),
        'display_name': session.get('display_name'),
        'is_admin': session.get('is_admin', False),
        'tier': session.get('tier', 'free'),
    }
