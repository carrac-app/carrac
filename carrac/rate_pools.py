"""
Rate limit pool classification and API key Premium gate.

Classifies each request into one of:
  - web_session: browser users with session cookies
  - api_key: REST API / MCP consumers with X-API-Key header
  - widget_token: mobile widget consumers with Bearer token
  - anonymous: no auth

Pool-namespaced keys ensure web, API, and widget consumers have
independent rate limit counters (e.g. web:user:42 vs api:key:abc12345).

API key pool is open to every authenticated tier (free invite-only beta —
free users mint + use keys for MCP/TUI). Tier differentiation is per-tool
(min_tier) + per-tier rate budgets, NOT an all-or-nothing API gate. The
allow-list below is a defensive sanity check: an unknown/disabled tier is
refused a key rather than silently granted access.

Classification logic moved to ``carrac/auth/rate_classify.py``
so Flask and FastAPI runtimes share a single algorithm. This wrapper fans
the resulting ``RateContext`` out onto ``flask.g`` for the 13 existing
readers in alerts/composite/plays/MCP/usage that still read ``flask.g.*``
directly.
"""

from flask import g, session, jsonify

from carrac.auth.rate_classify import FlaskRequestAdapter, _classify_pure

# Tiers permitted to mint + use API keys. Open to all real tiers for the
# free invite-only beta; this is an allow-list, not a paywall — paid value is
# enforced per-tool (min_tier) and per-tier rate budgets.
from carrac.tiers import API_ALLOWED_TIERS


def classify_request():
    """Classify the current request and stamp the result onto ``flask.g``.

    Sets on flask.g:
      - rate_pool: 'api_key' | 'widget_token' | 'web_session' | 'anonymous'
      - rate_key: pool-namespaced string for rate limit counting
      - rate_tier: tier of the caller (api_key pool: key's user; web: session)
      - api_key_user_id / api_key_tier / api_key_scope / api_key_internal: api_key pool only
    """
    ctx = _classify_pure(FlaskRequestAdapter(), lambda: dict(session))
    g.rate_pool = ctx.pool
    g.rate_key = ctx.key
    g.rate_tier = ctx.tier
    if ctx.pool == 'api_key':
        # Mirror the legacy behaviour: only set api_key_* attrs for the
        # api_key pool. Per-user keys carry user_id; the shared admin key
        # leaves user_id None.
        if ctx.api_key_user_id is not None:
            g.api_key_user_id = ctx.api_key_user_id
        g.api_key_tier = ctx.api_key_tier
        g.api_key_scope = ctx.api_key_scope
        g.api_key_internal = ctx.api_key_internal


def check_api_tier_gate():
    """Refuse API-key access for tiers outside the allow-list.

    Returns None to allow, or (response, status_code) to deny. Open to every
    real tier (free included) — only an unknown/disabled tier is refused.
    """
    if not hasattr(g, 'rate_pool') or g.rate_pool != 'api_key':
        return None

    tier = getattr(g, 'rate_tier', 'free')
    if tier in API_ALLOWED_TIERS:
        return None

    return jsonify({
        'error': 'API access is not available for this account.',
        'tier': tier,
    }), 403
