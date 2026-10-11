"""
Carrac — Flask application factory.

Module-load-time bootstrap (Sentry, PostHog, logging) runs once per process.
This ensures both the web container (which calls create_app()) and the worker
container (which imports carrac.precompute without calling create_app())
get consistent error tracking, analytics, and log formatting.
"""

import logging
import os
import sys
import time as _time_module
import numpy as np

from carrac.site_url import site_base_url
from carrac.version import APP_VERSION

# Env-tunable threshold for slow-request warnings (both Flask and
# FastAPI surfaces). /api/screenshot legitimately runs ~5 s — that's fine,
# informative, low volume; no exclusions.
SLOW_REQUEST_THRESHOLD_S: float = float(os.getenv('SLOW_REQUEST_THRESHOLD_S', '2.0'))

# Pages a landing-only host (LANDING_HOSTS) hands over to the app's address.
_SIGN_IN_PATHS = frozenset({'/login', '/register', '/forgot-password', '/reset-password'})


# ---------------------------------------------------------------------------
# Module-load bootstrap (Sentry / PostHog / logging)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logging.getLogger('flask_wtf.csrf').setLevel(logging.WARNING)

logger = logging.getLogger('carrac')

# Scrub PII before any event reaches Bugsink/Sentry.
# These live at module scope, not inside the `if _SENTRY_DSN`
# block, so they can be tested without a DSN configured. A scrubber that only
# exists in production is a scrubber nobody has ever run.
_PII_KEYS = frozenset({
    'email', 'password', 'authorization', 'cookie',
    'set-cookie', 'x-api-key', 'token',
})

# Body keys that must never reach the error tracker. Matched
# case-insensitively as a substring, so `api_secret`, `API_Secret` and
# `binance_api_secret` are all caught.
_SECRET_BODY_KEYS = (
    'pass' + 'word', 'secret', 'api_key', 'apikey', 'token',
    'credential', 'private_key', 'authorization',
)

_FILTERED = '[Filtered]'


def _scrub_body(value, depth=0):
    """Recursively redact secret-bearing keys from a captured request body.

    Returns the scrubbed value. A string BODY that cannot be parsed as JSON is
    dropped wholesale rather than passed through — if we cannot see inside it,
    we cannot say it is safe.
    """
    if depth > 6:
        return _FILTERED
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and any(s in k.lower() for s in _SECRET_BODY_KEYS):
                out[k] = _FILTERED
            else:
                out[k] = _scrub_body(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub_body(v, depth + 1) for v in value]
    if isinstance(value, str) and depth == 0:
        # Only the body ITSELF may arrive as a raw string. Parse it so the keys
        # inside can be redacted; if it will not parse we cannot inspect it, so
        # it is dropped rather than shipped. Nested strings are ordinary values
        # (an exchange name, a label) and pass through untouched — a scrubber
        # that blanks every string makes the report useless and gets disabled.
        import json as _json
        try:
            parsed = _json.loads(value)
        except (ValueError, TypeError):
            return _FILTERED
        if isinstance(parsed, (dict, list)):
            return _scrub_body(parsed, depth + 1)
        return _FILTERED
    return value


def _sentry_before_send(event, hint):
    """Strip known PII from request headers, cookies, query string and body."""
    request_info = event.get('request', {})
    # Scrub headers
    headers = request_info.get('headers', {})
    if isinstance(headers, dict):
        for k in list(headers.keys()):
            if k.lower() in _PII_KEYS:
                headers[k] = _FILTERED
    # Scrub cookies
    if 'cookies' in request_info:
        request_info['cookies'] = _FILTERED
    # Scrub query string for sensitive params (key / pw / token).
    qs = request_info.get('query_string', '') or ''
    _sensitive_qs_params = ('key', 'pass' + 'word', 'token')
    if any(p + '=' in qs for p in _sensitive_qs_params):
        import re as _re
        qs = _re.sub(r'(key|pass' + r'word|token)=[^&]*',
                     r'\1=' + _FILTERED, qs)
        request_info['query_string'] = qs
    # Scrub the BODY. send_default_pii=False does not cover this —
    # it only gates cookies and IP — so an exchange api_secret / password /
    # bot_token posted as JSON would reach Bugsink in the clear.
    if 'data' in request_info:
        request_info['data'] = _scrub_body(request_info['data'])

    # Collapse third-party websocket-client errors onto ONE issue
    # per condition.
    #
    # websocket-client logs handshake failures through its own `websocket`
    # logger at ERROR, so the logging integration turns each one into an event,
    # and it stringifies the exception with the whole nginx response header dict
    # inline — including a `date:` header. Bugsink groups by message, so every
    # single event became its own permanent issue: 614 issues for ONE condition
    # (rate-limited Finnhub handshakes), 66% of the tracker, which is what made
    # it unreadable and therefore untriaged.
    #
    # The headers are not lost, they move to `extra` where volatile detail
    # belongs. The event still reports — this is a grouping fix, not a mute.
    if event.get('logger') == 'websocket':
        entry = event.get('logentry')
        if isinstance(entry, dict):
            msg = entry.get('message') or ''
            # websocket-client's separator between status line and headers.
            head, sep, tail = msg.partition(' -+-+- ')
            if sep:
                entry['message'] = head
                extra = event.setdefault('extra', {})
                if isinstance(extra, dict):
                    extra['websocket_response_headers'] = tail[:1000]
    return event


def _sentry_init_kwargs(dsn):
    """The exact kwargs passed to sentry_sdk.init.

    Extracted so the guards can be asserted rather than assumed — see
    tests/test_sentry_body_scrub.py::TestTransportIsWired.
    """
    return dict(
        dsn=dsn,
        # Without a release, "is this still happening on the current build?"
        # is unanswerable from the issue page.
        release=APP_VERSION,
        send_default_pii=False,  # no auto-attach of headers/cookies/IP
        traces_sample_rate=0,
        before_send=_sentry_before_send,
        # Belt to _scrub_body's braces — never ask for bodies at
        # all. The SDK default is "medium" (~10KB captured), which is how the
        # exchange-credential route could ship a plaintext Binance secret.
        max_request_body_size='never',
    )


def _maybe_init_sentry(dsn, argv):
    """Initialise Sentry iff `dsn` is set AND `argv` names a real entrypoint.

    Importing `carrac` arms Sentry as a package side effect, so
    an ad-hoc `docker exec carrac python -c '...'` / heredoc / /tmp scratch
    script would report every typo as a "prod exception" too. Gating the *decision to call
    sentry_sdk.init at all* (rather than filtering per-event in
    before_send) means a scratch process never arms the SDK in the first
    place, and it keeps this function testable without reloading the whole
    `carrac` package or touching os.environ.

    Returns True if Sentry was armed, False if suppressed.
    """
    if not dsn:
        return False
    from carrac.observability import should_report_event
    if not should_report_event(argv):
        return False
    import sentry_sdk
    from sentry_sdk.integrations.logging import ignore_logger

    sentry_sdk.init(**_sentry_init_kwargs(dsn))
    for _name in _SENTRY_IGNORED_LOGGERS:
        ignore_logger(_name)
    return True


# Third-party loggers that report a condition we already handle AND already
# log ourselves. Sentry's logging integration turns any ERROR record into an
# event, so a library that disagrees with us about severity manufactures issues.
#
# `websocket` is the client library under data_sources/finnhub_ws.py. That
# module logs the disconnect itself at WARNING and reconnects with exponential
# backoff (finnhub_ws.py::_on_error and its reconnect loop) — the library then logs the SAME
# disconnect at ERROR. Left unsilenced, that produced thousands of Bugsink events
# for a handled reconnect, against a 3000-event project cap.
#
# Deliberately narrow. Only libraries we do not control belong here; silencing
# one of our own loggers would hide a real fault, which is why a test asserts
# nothing starting with 'carrac' is in this set. Our warning still reaches
# docker logs and still attaches as a Sentry breadcrumb — only the duplicate
# ERROR event stops.
_SENTRY_IGNORED_LOGGERS = ('websocket',)

_SENTRY_DSN = os.environ.get('SENTRY_DSN')
if _SENTRY_DSN:
    # One line at startup either way, so a wrongly-suppressed
    # real entrypoint (a gap in the deny-by-shape rule) is discoverable from
    # docker logs instead of silently reporting nothing forever.
    _entrypoint = sys.argv[0] if sys.argv else '(empty argv)'
    if _maybe_init_sentry(_SENTRY_DSN, sys.argv):
        logger.info("Sentry ARMED — reporting to Bugsink (entrypoint=%r, release=%s)",
                     _entrypoint, APP_VERSION)
    else:
        logger.warning(
            "Sentry SUPPRESSED — entrypoint %r matches the "
            "scratch-script shape (carrac/observability.py:should_report_event: "
            "-c / - / <stdin>, or a path under /tmp, /dev, /var/tmp), so no "
            "events will report from this process. If this is a real "
            "service and not a one-off script, it should not be launched "
            "from one of those paths.",
            _entrypoint)

# C2: no official `posthog` Python SDK init here — verified vestigial (the
# only server-side PostHog calls in this codebase go through the raw-HTTP
# helper in carrac/integrations.py, not the `posthog` package). Config.
# POSTHOG_API_KEY / POSTHOG_HOST still feed the client-side snippet via the
# _inject_analytics context processor below.


from flask import Flask, jsonify
from flask.json.provider import DefaultJSONProvider

from carrac.config import Config


class NumpyJSONProvider(DefaultJSONProvider):
    """JSON provider that handles numpy types."""

    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def _init_session(app):
    """Configure server-side sessions (Redis DB 1 or filesystem fallback)."""
    try:
        from flask_session import Session
        if app.config.get('SESSION_TYPE') == 'redis':
            try:
                import redis
                session_url = app.config.get('REDIS_SESSION_URL') or app.config.get('REDIS_URL', 'redis://localhost:6379/0')
                app.config['SESSION_REDIS'] = redis.from_url(session_url)
                logger.info("Flask-Session configured with Redis backend (DB 1)")
            except Exception as e:
                logger.warning(f"Redis unavailable for sessions, using filesystem: {e}")
                app.config['SESSION_TYPE'] = 'filesystem'
                app.config['SESSION_FILE_DIR'] = '/tmp/flask_sessions'
        Session(app)
    except ImportError:
        logger.info("flask-session not installed, using default cookie sessions")


def create_app(config_class=Config):
    """Application factory."""
    import time
    import uuid
    from datetime import datetime as _dt

    from flask import g, request, redirect, url_for, session, render_template
    from werkzeug.middleware.proxy_fix import ProxyFix

    from changelog import APP_VERSION, CHANGELOG
    from security import configure_flask_security, add_security_headers, generate_csp_nonce
    from carrac.config import Config as _Cfg
    from carrac.extensions import csrf, limiter
    from carrac.rate_pools import classify_request, check_api_tier_gate
    from carrac.rest_rate_limit import enforce_rest_rate_limit

    app = Flask(
        __name__,
        template_folder='../templates',
        static_folder='../static',
    )
    app.config.from_object(config_class)
    app.config['FINNHUB_API_KEY'] = os.environ.get('FINNHUB_API_KEY', '')

    # Trust exactly ONE proxy hop for X-Forwarded-For. The front nginx
    # appends $remote_addr (already Cloudflare-real-IP-resolved via
    # conf.d/cloudflare-real-ip.conf) as the rightmost XFF entry, so hop 1 is
    # the only nginx-controlled value. x_for=2 trusted the second-from-right
    # entry, which is attacker-suppliable on direct-to-origin requests that
    # bypass Cloudflare. Mirrors ProxyHeadersMiddleware(trusted_hosts=
    # "127.0.0.1") on the FastAPI side.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Service container (db, calculators, scorers) — built once, attached to
    # app.extensions AND mirrored to carrac.services._fallback_container so
    # non-Flask contexts (worker, quant collectors) resolve the same instance.
    from carrac.services import install_on_app
    install_on_app(app)

    # JSON provider for numpy types
    app.json_provider_class = NumpyJSONProvider
    app.json = NumpyJSONProvider(app)

    # CSRF + rate limiter
    csrf.init_app(app)
    if _Cfg._redis_available():
        app.config['RATELIMIT_STORAGE_URI'] = app.config.get('REDIS_URL', 'memory://')
    else:
        app.config['RATELIMIT_STORAGE_URI'] = 'memory://'
    app.config['RATELIMIT_DEFAULT'] = "10000 per day;3000 per hour"
    limiter.init_app(app)

    # Flask-Session + security config
    _init_session(app)
    configure_flask_security(app)

    # Static assets are public and never need a session cookie. With
    # SESSION_REFRESH_EACH_REQUEST=True, Flask-Session otherwise re-emits a
    # Set-Cookie on every /static/* response — and Cloudflare refuses to
    # edge-cache (or Brotli-compress) any response carrying Set-Cookie
    # (cf-cache-status: BYPASS). That forced every visitor to re-download the
    # full JS payload uncompressed from origin on every load. Skipping the
    # session save for static requests lets CF cache + compress them.
    # NB: this must suppress the *save* — stripping the header in an
    # after_request hook does not work, since Flask saves the session cookie
    # AFTER all after_request hooks have run.
    _orig_save_session = app.session_interface.save_session

    def _save_session_skip_static(*args, **kwargs):
        if request.path.startswith('/static/'):
            return
        return _orig_save_session(*args, **kwargs)

    app.session_interface.save_session = _save_session_skip_static

    # -----------------------------------------------------------------------
    # Before-request hooks
    # -----------------------------------------------------------------------
    @app.before_request
    def _record_request_start():
        """Stamp the monotonic start time for slow-request detection."""
        g._t0 = _time_module.monotonic()

    @app.before_request
    def _make_session_permanent():
        # Static assets don't use sessions — skip to avoid a needless Redis
        # session load on every asset request (and to keep them cookie-free).
        if request.path.startswith('/static/'):
            return
        session.permanent = True

    @app.before_request
    def _redirect_landing_host_to_app():
        """Send sign-in pages and signed-in visitors from a landing-only host to
        the app's own address.

        LANDING_HOSTS (comma-separated hostnames) names hosts that serve only
        the public site; unset, nothing is redirected.
        """
        landing_hosts = {
            h.strip().lower() for h in os.getenv('LANDING_HOSTS', '').split(',') if h.strip()
        }
        if not landing_hosts or request.host.split(':')[0].lower() not in landing_hosts:
            return None
        if request.path in _SIGN_IN_PATHS or 'user_id' in session:
            target = request.full_path.rstrip('?')
            return redirect(f'{site_base_url()}{target}', code=302)
        return None

    @app.before_request
    def _check_session_idle():
        """Session lifetime policy: 30-day absolute, 14-day idle, 5-min tier refresh.

        Bypassed under TESTING to keep the test fixture's ``sess['user_id']``
        injection working without having to seed real user rows.
        """
        if app.config.get('TESTING'):
            return
        if 'user_id' in session:
            now = time.time()
            created = session.get('_created_at', 0)
            if created and (now - created) > 30 * 86400:
                session.clear()
                return
            last_active = session.get('_last_active')
            if last_active and (now - last_active) > 14 * 86400:
                session.clear()
                return
            last_check = session.get('_active_check', 0)
            if now - last_check > 300:
                try:
                    from user_db import user_db as _udb
                    user = _udb.get_user_by_id(session['user_id'])
                    if not user or not user.get('is_active', True):
                        session.clear()
                        return
                    if not session.get('is_admin') and not session.get('_impersonating_tier'):
                        expiry = _udb.check_subscription_expiry(session['user_id'])
                        db_tier = expiry.get('tier', 'free')
                        if db_tier != session.get('tier'):
                            session['tier'] = db_tier
                except Exception as e:
                    logger.warning(f"Session user check failed for user_id={session.get('user_id')}: {e}")
                session['_active_check'] = now
            session['_last_active'] = now

    @app.before_request
    def _set_request_id():
        g.request_id = request.headers.get('X-Request-ID', str(uuid.uuid4())[:8])
        # Attach user + request_id to Sentry/Bugsink context so when
        # an exception fires we can answer "which user hit this, and which
        # line in the log covers it". Cheap — only fires when SENTRY_DSN is set.
        if _SENTRY_DSN:
            try:
                import sentry_sdk
                sentry_sdk.set_tag('request_id', g.request_id)
                uid = session.get('user_id') or getattr(g, 'api_key_user_id', None)
                if uid:
                    # Privacy fix: do NOT attach email — the privacy policy
                    # states error tracking carries no PII. id + tier are
                    # enough to correlate an error with "which user hit this".
                    sentry_sdk.set_user({
                        'id': uid,
                        'tier': session.get('tier') or getattr(g, 'api_key_tier', None),
                    })
            except Exception:
                # Never let telemetry break the request path.
                pass

    @app.before_request
    def _set_csp_nonce():
        g.csp_nonce = generate_csp_nonce()

    @app.before_request
    def _classify_rate_pool():
        classify_request()

    @app.before_request
    def _enforce_api_key_scope():
        """Refuse writes for read-only API keys.

        ``verify_api_key`` (called inside ``classify_request``/elsewhere)
        sets ``g.api_key_scope`` for cvx_k_ keys. A scope of ``read-only``
        was stored by ``apikeys/routes.py`` but never enforced — a key
        labelled read-only could still POST/PUT/DELETE. This hook closes
        that gap before any write handler sees the request.
        """
        if g.get('api_key_scope') == 'read-only' and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            return jsonify({
                'error': 'read-only API key cannot perform write operations'
            }), 403

    @app.before_request
    def _enforce_api_tier_gate():
        result = check_api_tier_gate()
        if result is not None:
            return result

    @app.before_request
    def _enforce_rest_rate_limit():
        result = enforce_rest_rate_limit()
        if result is not None:
            return result

    @app.after_request
    def _track_api_usage(response):
        from carrac.usage import track_api_request
        return track_api_request(response)

    # -----------------------------------------------------------------------
    # Context processors
    # -----------------------------------------------------------------------
    @app.context_processor
    def _inject_csp_nonce():
        return dict(csp_nonce=getattr(g, 'csp_nonce', ''))

    @app.context_processor
    def _inject_site_url():
        # The instance's own address, for every connect/copy-paste snippet a
        # template shows (MCP endpoint, API base, curl examples).
        from carrac.site_url import site_base_url, site_host, support_email
        return dict(site_url=site_base_url(), site_host=site_host(),
                    support_email=support_email())

    @app.context_processor
    def _inject_app_version():
        return dict(app_version=APP_VERSION, changelog=CHANGELOG,
                    sentry_browser_dsn=(os.environ.get('SENTRY_BROWSER_DSN') or '').strip())

    @app.context_processor
    def _inject_reports_paused():
        # Landing card, guide prose and the brief pages all hide or
        # re-label their "today's brief" surfaces when no report is produced.
        from morning_engine.config import reports_paused
        return dict(reports_paused=reports_paused())

    @app.context_processor
    def _inject_mcp_tool_counts():
        # Every page that quoted a tool count hardcoded it, and
        # they had drifted to three different wrong numbers at once (33, 76 and
        # "100+") across /workflow, the 404 nav, /integrations and the landing
        # pages. Derived from the live registry so adding or consolidating a
        # tool can never leave a stale figure on a marketing page again.
        try:
            import carrac.mcp.tools  # noqa: F401 — triggers @mcp_tool registration
            from carrac.mcp.registry import TOOL_REGISTRY
            total = len(TOOL_REGISTRY)
            free = sum(1 for m in TOOL_REGISTRY.values() if m.get('min_tier') == 'free')
        except Exception:
            # A marketing page must still render if the registry can't load.
            return dict(mcp_tool_count=None, mcp_free_tool_count=None)
        return dict(mcp_tool_count=total, mcp_free_tool_count=free)

    @app.context_processor
    def _inject_payments():
        # Purchase / upsell CTAs (e.g. the "Become Premium" button) only render
        # when Paddle checkout is actually live. During the free beta Paddle is
        # unconfigured, so payments_enabled is False and those CTAs hide — no
        # dead-end checkout. Auto-returns once Paddle is wired up.
        return dict(payments_enabled=bool(Config.PADDLE_API_KEY and Config.PADDLE_CLIENT_TOKEN))

    @app.context_processor
    def _inject_legal_entity():
        # Every legal page + footer pulls the entity from one place.
        from carrac.legal_entity import get_legal_entity, disclosure_sentence
        return dict(
            legal_entity=get_legal_entity(),
            legal_disclosure=disclosure_sentence(),
        )

    @app.context_processor
    def _inject_analytics():
        # Propagate Cloudflare's cf-ipcountry header so the template
        # can default opt-in/out for session replay by geography. EU
        # member states default to opt-out until the compliant consent
        # banner ships.
        try:
            from flask import request as _req
            cf_country = (_req.headers.get('CF-IPCountry') or '').upper()
        except Exception:
            cf_country = ''
        return dict(
            posthog_api_key=Config.POSTHOG_API_KEY,
            posthog_host=Config.POSTHOG_HOST,
            cf_ipcountry=cf_country,
        )

    @app.context_processor
    def _inject_ph_identity():
        # C2: universal identify() source of truth — reuses the same session
        # keys as the request_id/Sentry user tag above (user_id / tier),
        # session-only reads, no DB hit. No email: PostHog only ever gets the
        # hashed id + events per the privacy policy. If
        # tier isn't in the session we don't fabricate one (no DB lookup just
        # for analytics); the template renders it as null rather than a
        # guessed default.
        user_id = session.get('user_id')
        if not user_id:
            return dict(ph_user_id=None, ph_user_tier=None)
        return dict(
            ph_user_id=user_id,
            ph_user_tier=session.get('tier'),
        )

    @app.context_processor
    def _inject_cache_buster():
        import hashlib

        # Memoized per (path, mtime, size): index.html alone calls this 73×,
        # and without the memo every HTML render re-reads and re-hashes ~3.6MB
        # of static files. mtime-keyed so bind-mounted files still
        # bust correctly after a sync without a process restart.
        _memo = {}

        def cache_version(filename):
            filepath = os.path.join(app.static_folder, filename)
            try:
                st = os.stat(filepath)
            except OSError:
                return str(int(time.time()))
            key = (filepath, st.st_mtime_ns, st.st_size)
            cached = _memo.get(key)
            if cached is None:
                try:
                    with open(filepath, 'rb') as f:
                        cached = hashlib.md5(f.read(), usedforsecurity=False).hexdigest()[:8]
                except OSError:
                    return str(int(time.time()))
                _memo[key] = cached
            return cached
        return dict(cache_version=cache_version)

    # -----------------------------------------------------------------------
    # After-request hooks
    # -----------------------------------------------------------------------
    @app.after_request
    def _apply_security_headers(response):
        return add_security_headers(response)

    @app.after_request
    def _add_request_id_header(response):
        if hasattr(g, 'request_id'):
            response.headers['X-Request-ID'] = g.request_id
        return response

    @app.after_request
    def _add_api_version_header(response):
        from carrac.openapi import API_VERSION
        response.headers['X-API-Version'] = API_VERSION
        return response

    @app.after_request
    def _add_cache_control(response):
        from carrac.cache_headers import apply_cache_headers
        return apply_cache_headers(response, request.path, request.method, response.content_type)

    @app.after_request
    def _warn_slow_request(response):
        """Emit a WARNING when a request exceeds SLOW_REQUEST_THRESHOLD_S."""
        try:
            t0 = getattr(g, '_t0', None)
            if t0 is not None:
                elapsed = _time_module.monotonic() - t0
                if elapsed > SLOW_REQUEST_THRESHOLD_S:
                    logger.warning(
                        "SLOW %s %s %.2fs status=%s",
                        request.method,
                        request.path,
                        elapsed,
                        response.status_code,
                    )
        except Exception:
            pass
        return response

    # -----------------------------------------------------------------------
    # Error handlers
    # -----------------------------------------------------------------------
    @app.errorhandler(400)
    def _handle_csrf_error(e):
        if 'CSRF' in str(e) or 'csrf' in str(e):
            if request.path.startswith('/api/') or request.is_json:
                return jsonify({'error': 'CSRF validation failed'}), 400
            session.clear()
            return redirect(url_for('auth.login', csrf_expired='1'))
        return e

    @app.errorhandler(404)
    def _handle_not_found(e):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not found'}), 404
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def _handle_server_error(e):
        logger.error(f"Internal server error: {e}")
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Internal server error'}), 500
        return render_template('login.html', error='Something went wrong'), 500

    # -----------------------------------------------------------------------
    # Teardown — clean up stale PG connections stuck in INERROR state
    # -----------------------------------------------------------------------
    @app.teardown_request
    def _cleanup_db_connections(exc):
        from carrac.db_pool import get_db_type
        if get_db_type() != 'postgresql':
            return
        try:
            from user_db import user_db as _udb
            local = _udb._local
            if hasattr(local, 'pg_conn') and local.pg_conn and not local.pg_conn.closed:
                if local.pg_conn.info.transaction_status == 4:  # TRANSACTION_STATUS_INERROR
                    local.pg_conn.rollback()
        except Exception:
            pass
        try:
            from carrac.content_db import get_content_db
            cdb = get_content_db()
            local = cdb._local
            if hasattr(local, 'pg_conn') and local.pg_conn and not local.pg_conn.closed:
                if local.pg_conn.info.transaction_status == 4:
                    local.pg_conn.rollback()
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Sentry tunnel (proxies browser error reports to local Bugsink)
    # -----------------------------------------------------------------------
    # Always registered; answers 404 unless BOTH the server DSN (where reports
    # go) and SENTRY_BROWSER_DSN (what the page may send) are configured.
    import requests as _requests

    def _parse_dsn(dsn):
        """-> (scheme, host, public_key, project_id) or None."""
        from urllib.parse import urlsplit
        try:
            u = urlsplit((dsn or '').strip())
            project = u.path.strip('/').split('/')[-1]
            if u.scheme in ('http', 'https') and u.hostname and u.username and project.isdigit():
                return (u.scheme, u.netloc.rsplit('@', 1)[-1], u.username, project)
        except ValueError:
            pass
        return None

    @app.route('/api/error-tunnel', methods=['POST'])
    @csrf.exempt
    @limiter.limit("30 per minute")
    def _sentry_tunnel():
        server = _parse_dsn(_SENTRY_DSN)
        browser = _parse_dsn(os.environ.get('SENTRY_BROWSER_DSN'))
        if not server or not browser:
            return '', 404
        if request.content_length and request.content_length > 100_000:
            return '', 413
        envelope = request.get_data(cache=False)
        if len(envelope) > 100_000:
            return '', 413
        # Not an open relay: the envelope header must name exactly the
        # configured browser DSN (same host, key and project).
        try:
            import json as _json
            header = _json.loads(envelope.split(b'\n', 1)[0])
            claimed = _parse_dsn(header.get('dsn'))
        except (ValueError, AttributeError):
            claimed = None
        if claimed != browser:
            return '', 400
        scheme, host, _key, project = server
        try:
            _resp = _requests.post(
                f'{scheme}://{host}/api/{project}/envelope/',
                data=envelope,
                headers={'Content-Type': 'application/x-sentry-envelope'},
                timeout=5,
            )
            if not (200 <= _resp.status_code < 300):
                logger.warning(
                    "error-tunnel: Sentry endpoint returned non-2xx status=%s body=%s",
                    _resp.status_code, _resp.text[:200],
                )
        except Exception as e:
            # Never let telemetry break the client's error path, but a
            # broken forward pipeline must be visible in logs.
            logger.warning("error-tunnel: failed to forward envelope: %s", e)
        return '', 200

    # -----------------------------------------------------------------------
    # Jinja filter
    # -----------------------------------------------------------------------
    @app.template_filter('fmtdt')
    def _fmtdt(val, default=''):
        if not val:
            return default
        if isinstance(val, _dt):
            return val.strftime('%Y-%m-%d %H:%M')
        s = str(val)
        return s[:16].replace('T', ' ')

    @app.template_filter('fmtdate')
    def _fmtdate(val, default=''):
        """Render a DB timestamp as ``YYYY-MM-DD``, regardless of whether the
        driver handed back a ``datetime`` (Postgres, prod) or a plain string
        (SQLite, tests, or a JSON-loaded snapshot). A bare
        ``created_at[:10]`` in Jinja worked only for the string case and
        500'd in production with ``TypeError: 'datetime.datetime' object is not
        subscriptable``. Registered app-wide (not on the reports blueprint)
        so admin/share templates can use it without the reports blueprint
        being loaded.
        """
        if not val:
            return default
        if isinstance(val, _dt):
            return val.strftime('%Y-%m-%d')
        return str(val)[:10]

    # -----------------------------------------------------------------------
    # Cache manager (multi-user API rate-limit protection)
    # -----------------------------------------------------------------------
    from cache_manager import initialize_cache
    if os.environ.get('TESTING', '').lower() in ('1', 'true', 'yes'):
        # initialize_cache() populates the price cache SYNCHRONOUSLY
        # — one full Binance 24h ticker book (weight 80) + CoinPaprika per
        # create_app(). ~1,440 test apps per run got the CI host's IP weight-banned.
        logger.info("TESTING mode — initial cache population skipped")
    else:
        logger.info("Initializing global cache manager...")
        try:
            initialize_cache(start_refresh=False)
            logger.info("Global cache manager initialized (background refresh deferred to lock holder)")
        except Exception as _cache_err:
            logger.warning(f"Cache manager startup failed ({_cache_err}) — app will start with empty cache")

    # -----------------------------------------------------------------------
    # Blueprints
    # -----------------------------------------------------------------------
    _register_blueprints(app, csrf, limiter)

    # -----------------------------------------------------------------------
    # Background threads (file-lock gated; skipped when WORKER_MODE=true)
    # -----------------------------------------------------------------------
    _bootstrap_background_threads(app)

    return app


def _register_blueprints(app, csrf, limiter):
    """Register all application blueprints.

    Idempotency guard: allows tests to call create_app() on an app instance
    that already has blueprints registered. Production uses only create_app().
    """
    if 'auth' in getattr(app, 'blueprints', {}):
        logger.info("Blueprints already registered on this app — short-circuiting")
        return

    def _chart_limit():
        """Higher limits for logged-in users, strict for anonymous."""
        from flask import session
        if session.get('user_id'):
            return "6000 per hour"
        return "500 per hour"

    # Auth
    from carrac.auth.routes import auth_bp
    app.register_blueprint(auth_bp)

    # No social auth: it was never enabled in production and the Flask
    # handlers were dead code.
    # If social login is revived, port directly to FastAPI via authlib's
    # starlette_client integration.

    # Dashboard
    from carrac.dashboard.routes import dashboard_bp
    app.register_blueprint(dashboard_bp)
    csrf.exempt(dashboard_bp)

    # Admin
    from carrac.admin.routes import admin_bp
    app.register_blueprint(admin_bp)
    limiter.limit("30 per minute")(admin_bp)

    # Assets (watchlist, cache stats, /api/assets precompute consumer)
    from carrac.assets.routes import assets_bp
    app.register_blueprint(assets_bp)
    csrf.exempt(assets_bp)

    # Alerts
    from carrac.alerts.routes import alerts_bp
    app.register_blueprint(alerts_bp)

    # Sentiment
    from carrac.sentiment.routes import sentiment_bp
    app.register_blueprint(sentiment_bp)

    # Market analytics (breadth, etc.)
    from carrac.market.routes import market_bp
    app.register_blueprint(market_bp)

    # News
    from carrac.news import news_bp
    app.register_blueprint(news_bp)

    # Screenshots
    from carrac.reports.screenshots import screenshots_bp
    app.register_blueprint(screenshots_bp)
    csrf.exempt(screenshots_bp)

    # Search
    from carrac.search.routes import search_bp
    app.register_blueprint(search_bp)

    # Charts
    from carrac.charts.routes import charts_bp
    app.register_blueprint(charts_bp)
    csrf.exempt(charts_bp)
    limiter.limit(_chart_limit)(charts_bp)
    logger.info("Charts blueprint registered")

    # Reports
    from carrac.reports.routes import reports_bp
    app.register_blueprint(reports_bp)
    logger.info("Reports blueprint registered")

    # Brief permalinks + archive (Task B) — a separate
    # blueprint so the honest-permalink /brief/<date> + /brief/archive routes
    # stay file-disjoint from carrac/reports/routes.py's live /brief cockpit.
    from carrac.reports.brief_archive import brief_archive_bp
    app.register_blueprint(brief_archive_bp)
    logger.info("Brief archive blueprint registered")

    # Billing (Paddle)
    from carrac.billing.routes import billing_bp
    app.register_blueprint(billing_bp)
    # CSRF per-route: webhook is @csrf.exempt; portal/status keep CSRF.
    limiter.limit("60 per minute")(billing_bp)
    logger.info("Billing blueprint registered")

    # AI assistant
    from carrac.ai import ai_bp
    app.register_blueprint(ai_bp)
    csrf.exempt(ai_bp)
    limiter.limit("5 per minute;60 per hour")(ai_bp)
    logger.info("AI assistant blueprint registered")

    # Journal
    from carrac.journal import journal_bp
    app.register_blueprint(journal_bp)
    csrf.exempt(journal_bp)
    limiter.limit("60 per minute")(journal_bp)
    logger.info("Journal blueprint registered")

    # Portfolio
    from carrac.portfolio.routes import portfolio_bp
    app.register_blueprint(portfolio_bp)

    # Widget
    from carrac.widget import widget_bp
    app.register_blueprint(widget_bp)
    csrf.exempt(widget_bp)
    limiter.limit("30 per minute")(widget_bp)
    logger.info("Widget blueprint registered")

    # API keys
    from carrac.apikeys import apikeys_bp
    app.register_blueprint(apikeys_bp)
    limiter.limit("10 per minute")(apikeys_bp)

    # Scanner
    from carrac.scanner import scanner_bp
    app.register_blueprint(scanner_bp)
    csrf.exempt(scanner_bp)
    logger.info("Scanner blueprint registered")

    # Plays
    from carrac.plays import plays_bp
    app.register_blueprint(plays_bp)
    csrf.exempt(plays_bp)
    limiter.limit("60 per minute")(plays_bp)
    logger.info("Plays blueprint registered")

    # Shareable Plays — public, no-auth read surface (/s/<slug>, /api/s/<slug>).
    # Writes (mint/revoke) live in the FastAPI plays router.
    from carrac.shares import shares_bp
    app.register_blueprint(shares_bp)
    csrf.exempt(shares_bp)
    limiter.limit("60 per minute")(shares_bp)
    logger.info("Shares blueprint registered")

    # Drawing sync
    from carrac.sync import sync_bp
    from carrac.sync.routes import poll_drawings  # noqa: F401
    app.register_blueprint(sync_bp)
    csrf.exempt(sync_bp)
    limiter.limit("60 per minute")(sync_bp)
    logger.info("Sync blueprint registered")

    # Legal (terms, privacy, disclaimer)
    from carrac.legal import legal_bp
    app.register_blueprint(legal_bp)
    logger.info("Legal blueprint registered")

    # Drawings + history
    from carrac.drawings import drawings_bp
    from carrac.history import history_bp
    app.register_blueprint(drawings_bp)
    csrf.exempt(drawings_bp)
    limiter.limit("2000 per hour")(drawings_bp)

    app.register_blueprint(history_bp)
    csrf.exempt(history_bp)
    limiter.limit("60 per hour")(history_bp)
    logger.info("Drawings/History blueprints registered")

    # OpenAPI docs
    from carrac.openapi.routes import openapi_bp
    app.register_blueprint(openapi_bp)

    # Usage telemetry
    from carrac.usage_routes import usage_bp
    app.register_blueprint(usage_bp)
    csrf.exempt(usage_bp)

    # MCP server is served by the FastAPI router.
    # carrac/fastapi_app/routers/mcp.py owns POST /mcp; no Flask blueprint.

    # Formula engine HTTP surface (preview / evaluate / lint)
    from carrac.formulas import formulas_bp
    app.register_blueprint(formulas_bp)
    csrf.exempt(formulas_bp)
    logger.info("Formulas blueprint registered")

    # Quant DB (optional — safe to fail without affecting core app)
    try:
        from quant.api.routes import quant_bp
        app.register_blueprint(quant_bp)
        csrf.exempt(quant_bp)
        limiter.limit("60 per minute")(quant_bp)
        logger.info("Quant blueprint registered")
    except Exception as e:
        logger.warning(f"Quant DB blueprint not loaded: {e}")


def _bootstrap_background_threads(app):
    """Start precompute / sentiment / backfill / WS workers (file-lock gated).

    Skipped entirely when ``WORKER_MODE=true`` — the dedicated carrac-worker
    container (``worker.py``) handles these in its own process. Locally (no
    worker container), one gunicorn worker acquires a file lock and becomes
    the compute host; the others short-circuit.
    """
    import atexit
    import fcntl
    import threading

    if os.environ.get('WORKER_MODE') == 'true':
        logger.info("WORKER_MODE — background threads delegated to worker process")
        return

    if os.environ.get('TESTING', '').lower() in ('1', 'true', 'yes'):
        logger.info("TESTING mode — background threads skipped")
        return

    try:
        _lock_file = open('/tmp/.carrac_precompute.lock', 'w')
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.info("Background threads skipped (another worker owns the lock)")
        return

    # This worker owns the lock — start everything
    from cache_manager import cache_manager
    import push_notifications
    from carrac.services import get_services
    from carrac.precompute import (
        _stop_precompute,
        _stop_sentiment,
        _startup_warmup,
        _start_sentiment_scheduler,
    )
    import carrac.precompute as _precompute

    db = get_services().db

    cache_manager.start_background_refresh(interval=30)
    logger.info("Cache background refresh started (this worker owns the lock)")

    _startup_thread = threading.Thread(target=_startup_warmup, daemon=True, name="StartupWarmup")
    _startup_thread.start()
    logger.info("Background pre-compute system initialized (this worker owns the lock)")

    _backfiller = None
    try:
        from backfill import HistoryBackfiller
        _backfiller = HistoryBackfiller(db)
        _backfiller.start()
        _precompute._backfiller = _backfiller
    except Exception as e:
        logger.warning(f"Backfiller failed to start: {e}")

    _start_sentiment_scheduler()
    logger.info("Sentiment scheduler started (this worker owns the lock)")

    # Hyperliquid WS live candles (Phase 2) — gated by env var
    _hl_ws_worker = None
    if os.environ.get('HYPERLIQUID_WS_ENABLED', '').lower() in ('1', 'true'):
        try:
            from data_sources.hyperliquid_ws import HyperliquidWSWorker
            from data_sources.hyperliquid import HyperliquidClient
            _hl_ws_worker = HyperliquidWSWorker(
                csv_cache=db.cache,
                hl_coins=HyperliquidClient.HL_COINS,
                k_prefix_translation=HyperliquidClient.K_PREFIX_TRANSLATION,
            )
            _hl_ws_worker.start()
            app.config['HL_WS_WORKER'] = _hl_ws_worker
            logger.info(f"HL WS worker started ({len(HyperliquidClient.HL_COINS)} coins)")
        except ImportError:
            logger.warning("HL WS worker: websocket-client not installed — REST-only mode")
        except Exception as e:
            logger.error(f"HL WS worker failed to start: {e}")
    else:
        logger.info("HL WS worker disabled (set HYPERLIQUID_WS_ENABLED=true to enable)")

    # CCXT WS live candles — gated by env var
    _ccxt_ws_worker = None
    if os.environ.get('CCXT_WS_ENABLED', '').lower() in ('1', 'true'):
        try:
            from data_sources.ccxt_ws import CcxtWSWorker
            _ccxt_ws_worker = CcxtWSWorker(csv_cache=db.cache, ccxt_client=db.ccxt)
            _ccxt_ws_worker.start()
            app.config['CCXT_WS_WORKER'] = _ccxt_ws_worker
            logger.info("CCXT WS worker started")
        except Exception as e:
            logger.error(f"CCXT WS worker failed to start: {e}")
    else:
        logger.info("CCXT WS worker disabled (set CCXT_WS_ENABLED=true to enable)")

    # Graceful shutdown — stop background threads cleanly on deploy/restart
    def _graceful_shutdown():
        logger.info("Graceful shutdown: stopping background threads...")
        _stop_precompute.set()
        _stop_sentiment.set()
        if _backfiller:
            _backfiller.stop()
        if _hl_ws_worker:
            _hl_ws_worker.stop()
        if _ccxt_ws_worker:
            _ccxt_ws_worker.stop()
        cache_manager.stop_background_refresh()
        try:
            push_notifications.flush_email_digests()
            logger.info("Graceful shutdown: email digests flushed")
        except Exception as e:
            logger.warning("Graceful shutdown: email flush failed: %s", e)
        if _precompute._precompute_thread and _precompute._precompute_thread.is_alive():
            _precompute._precompute_thread.join(timeout=3)
        try:
            fcntl.flock(_lock_file, fcntl.LOCK_UN)
            _lock_file.close()
        except Exception:
            pass
        logger.info("Graceful shutdown complete")

    atexit.register(_graceful_shutdown)
    logger.info("Graceful shutdown handler registered (lock-holder worker)")
