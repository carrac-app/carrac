"""
HTTP Cache-Control header policies for API endpoints.

Applied by the Flask after_request hook in carrac/__init__.py (``create_app``).

Policies are evaluated in order — first match wins. The default fallback
is no-cache (safe for any unmatched endpoint).
"""

# No-cache constant for reuse
_NO_CACHE = 'no-cache, no-store, must-revalidate, private'

# (url_prefix, method_or_None, cache_control_header)
# method=None matches ANY method for that prefix.
# Evaluated top-to-bottom, first match wins.
_API_CACHE_POLICIES = [
    # ---------------------------------------------------------------
    # Writes — never cache
    # ---------------------------------------------------------------
    ('/api/alerts',              'POST',   _NO_CACHE),
    ('/api/alerts/',             'PUT',    _NO_CACHE),
    ('/api/alerts/',             'DELETE',  _NO_CACHE),
    ('/api/plays',               'POST',   _NO_CACHE),
    ('/api/plays/',              'PUT',    _NO_CACHE),
    ('/api/plays/',              'DELETE',  _NO_CACHE),
    ('/api/drawing',             'POST',   _NO_CACHE),
    ('/api/drawing/',            'PUT',    _NO_CACHE),
    ('/api/drawing/',            'DELETE',  _NO_CACHE),
    ('/api/journal',             'POST',   _NO_CACHE),
    ('/api/journal/',            'PUT',    _NO_CACHE),
    ('/api/journal/',            'PATCH',  _NO_CACHE),
    ('/api/journal/',            'DELETE',  _NO_CACHE),
    ('/api/report/',             'POST',   _NO_CACHE),
    ('/api/push/',               'POST',   _NO_CACHE),
    ('/api/widget/tokens',       'POST',   _NO_CACHE),
    ('/api/widget/tokens/',      'DELETE',  _NO_CACHE),
    ('/api/billing/',            'POST',   _NO_CACHE),
    ('/api/notification-',       'POST',   _NO_CACHE),
    ('/api/user/',               'POST',   _NO_CACHE),
    ('/api/error-tunnel',        'POST',   _NO_CACHE),
    ('/api/csp-report',          'POST',   _NO_CACHE),
    ('/api/heartbeat',           'POST',   _NO_CACHE),
    ('/api/indicator-prefs',     'POST',   _NO_CACHE),
    ('/api/preferences/',        'POST',   _NO_CACHE),
    ('/api/feedback',            'POST',   _NO_CACHE),
    ('/api/account/',            'POST',   _NO_CACHE),
    ('/api/watchlist',           'POST',   _NO_CACHE),
    ('/api/admin/',              None,     _NO_CACHE),
    # AI endpoints — all methods, never cache (SSE, mutations, billing)
    ('/api/ai/',                 None,     _NO_CACHE),
    # Credits/engine — internal, never cache
    ('/api/credits/',            None,     _NO_CACHE),
    ('/api/engine/',             None,     _NO_CACHE),

    # ---------------------------------------------------------------
    # User-specific reads — private cache (browser can cache, nginx must not)
    # ---------------------------------------------------------------
    ('/api/alerts',              'GET',    'private, max-age=30'),
    ('/api/plays',               'GET',    'private, max-age=30'),
    ('/api/drawing',             'GET',    'private, max-age=30'),
    ('/api/journal',             'GET',    'private, max-age=30'),
    ('/api/watchlist',           'GET',    'private, max-age=30'),
    ('/api/widget/tokens',       'GET',    'private, max-age=30'),
    ('/api/widget/summary',      'GET',    'private, max-age=30'),
    ('/api/assets',              'GET',    'private, max-age=60'),
    ('/api/user/',               'GET',    'private, max-age=30'),
    ('/api/indicator-prefs',     'GET',    'private, max-age=30'),
    ('/api/preferences/',        'GET',    'private, max-age=30'),
    ('/api/notification-',       'GET',    'private, max-age=30'),
    ('/api/tier-info',           'GET',    'private, max-age=60'),
    ('/api/usage',               'GET',    'private, max-age=60'),
    ('/api/push/',               'GET',    'private, max-age=30'),
    ('/poll',                    'GET',    'private, max-age=5'),

    # ---------------------------------------------------------------
    # Shared reads — public cache with stale-while-revalidate
    # ---------------------------------------------------------------
    # Scanner responses are tier-gated (free=5 rows, premium=full): `public`
    # lets a shared nginx/CF cache cross-serve one user's tier-specific dataset
    # to every other caller (live-exploitable — same class as the report
    # leak below).  `private` keeps browser-side per-user caching
    # but forbids shared/proxy caches from storing the response.
    ('/api/scanner',             'GET',    'private, max-age=300, stale-while-revalidate=60'),
    # Confluence zones are non-user-varying market data (not tier-gated) → public.
    ('/api/confluence',          'GET',    'public, max-age=60, stale-while-revalidate=30'),
    # Liquidation heatmap cycles recompute every 15 min (worker) — non-user-varying.
    ('/api/liq-heatmap',         'GET',    'public, max-age=300, stale-while-revalidate=60'),
    # Tier-gated or per-user reads (quant_engine, member-only transitions, the
    # caller's watchlist, tier-limited timeframes, tier-only custom feeds) stay
    # `private`: a shared cache must not serve one caller's answer to another.
    ('/api/stream',              'GET',    'private, max-age=60, stale-while-revalidate=30'),
    ('/api/transitions',         'GET',    'private, max-age=60, stale-while-revalidate=30'),
    ('/api/market-sentiment',    'GET',    'public, max-age=300, stale-while-revalidate=60'),
    ('/api/market-news',         'GET',    'private, max-age=300, stale-while-revalidate=60'),
    ('/api/sentiment-history',   'GET',    'public, max-age=300, stale-while-revalidate=60'),
    # Report responses VARY BY AUTH (tier gating + per-user personal
    # sections): `public` would let the nginx proxy cache cross-serve one
    # caller's report body to every other caller for an hour (e.g. an
    # admin-warmed operator report served to the demo key). `private` keeps browser caching but forbids shared caches.
    ('/api/report/morning/latest', 'GET',  'private, max-age=300, stale-while-revalidate=60'),
    ('/api/report/morning/recent', 'GET',  'private, max-age=300, stale-while-revalidate=60'),
    ('/api/report/weekly/latest',  'GET',  'private, max-age=300, stale-while-revalidate=60'),
    ('/api/report/afternoon/latest', 'GET', 'private, max-age=300, stale-while-revalidate=60'),
    ('/api/quant/',              'GET',    'private, max-age=3600, stale-while-revalidate=300'),
    ('/api/symbol-lookup',       'GET',    'public, max-age=86400, stale-while-revalidate=3600'),
    ('/api/quote/',              'GET',    'public, max-age=60, stale-while-revalidate=30'),
    ('/api/weekly-signal',       'GET',    'public, max-age=300, stale-while-revalidate=60'),
    ('/api/relative-strength',   'GET',    'public, max-age=300, stale-while-revalidate=60'),
    ('/api/consensio/',          'GET',    'public, max-age=300, stale-while-revalidate=60'),
    ('/api/volume-profile/',     'GET',    'private, max-age=300, stale-while-revalidate=60'),
    ('/api/status',              'POST',   'public, max-age=60, stale-while-revalidate=30'),
    ('/api/health',              'GET',    'public, max-age=60, stale-while-revalidate=30'),
    # Widget public resources
    ('/api/push/vapid-key',      'GET',    'public, max-age=86400'),
    ('/widget/scriptable',       'GET',    'public, max-age=3600'),
    ('/widget/android/',         'GET',    'public, max-age=3600'),
    # OpenAPI spec & docs
    ('/api/openapi.json',        'GET',    'public, max-age=3600, stale-while-revalidate=300'),
    ('/api/docs',                'GET',    'private, max-age=60'),
    # Composite endpoints
    ('/api/asset/',              'GET',    'private, max-age=15'),
    ('/api/positions/live',      'GET',    'private, max-age=10'),
    ('/api/batch',               'GET',    'private, max-age=15'),
    ('/api/portfolio/summary',   'GET',    'private, max-age=10'),
    ('/api/portfolio/history',   'GET',    'private, max-age=300'),
]


def get_api_cache_header(path: str, method: str) -> str:
    """Look up Cache-Control header for an API endpoint.

    First-match evaluation against _API_CACHE_POLICIES.
    Returns no-cache for any unmatched path (safe fallback).
    """
    for prefix, rule_method, header in _API_CACHE_POLICIES:
        if path.startswith(prefix):
            if rule_method is None or rule_method == method:
                return header
    # Safe default: no cache for unmatched endpoints
    return _NO_CACHE


_NO_STORE_ERROR = 'no-store, no-cache, must-revalidate, private'


def apply_cache_headers(response, path: str, method: str, content_type: str):
    """Apply appropriate Cache-Control headers to a response.

    Call this from an after_request handler. Handles static, HTML, API,
    and chart endpoints (chart manages its own headers).

    Non-2xx responses always receive ``no-store`` regardless of the policy
    table — prevents nginx (or any shared cache) from caching error responses
    such as 401 that could then be served to authenticated callers.
    """
    if path in ('/static/service-worker.js', '/service-worker.js'):
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    elif path.startswith('/static/'):
        # Only 2xx static responses may be cached immutably.
        # A 404 (file briefly missing mid-deploy rsync --delete window, a
        # wiped preview dir, a typo'd link) must NOT be frozen in the browser
        # for a year — that strands the URL as permanently broken with no
        # server-side remedy. Mirror the non-2xx no-store guard the /api/
        # branch below already applies.
        status = getattr(response, 'status_code', 200)
        if 200 <= status < 300:
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            response.headers.pop('Pragma', None)
            response.headers.pop('Expires', None)
        else:
            response.headers['Cache-Control'] = _NO_STORE_ERROR
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
    elif content_type and 'text/html' in content_type:
        response.headers['Cache-Control'] = _NO_CACHE
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    elif path.startswith('/api/') or path.startswith('/poll') or path.startswith('/widget/'):
        # /api/chart/ and /api/chart-internal/ manage their own Cache-Control
        if not path.startswith('/api/chart/') and not path.startswith('/api/chart-internal/'):
            # Non-2xx responses must never be stored in a shared cache.
            # This prevents nginx from caching a 401 that was returned before
            # the caller's API key was validated, and then serving it to
            # subsequent authenticated requests.
            status = getattr(response, 'status_code', 200)
            if not (200 <= status < 300):
                response.headers['Cache-Control'] = _NO_STORE_ERROR
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
            else:
                header = get_api_cache_header(path, method)
                response.headers['Cache-Control'] = header
                if 'no-cache' in header:
                    response.headers['Pragma'] = 'no-cache'
                    response.headers['Expires'] = '0'
                else:
                    response.headers.pop('Pragma', None)
                    response.headers.pop('Expires', None)
    return response
