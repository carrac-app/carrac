"""The shared key authenticates ONLY via X-Internal-Key.

``SCREENSHOT_API_KEY`` is an internal deployment credential. If it were
accepted on the public, documented ``X-API-Key`` header by any auth seam
(resolving to admin tier), anyone who could read the server's ``.env`` could
replay it against the public API and the MCP server, unmetered
(``/api/report/morning/latest`` and ``POST /mcp``).

The rule, enforced at every seam: the shared key is honoured only when the
``X-Internal-Key`` header itself carries it; ``X-API-Key`` accepts per-user
``cvx_k_`` keys only. Per-user keys are unaffected on either header.

Seams covered here — ALL of them, because the first draft of the fix patched
only ``verify_api_key`` and that is the FALLBACK path: real traffic gets its
tier from ``classify_request`` (before_request on every Flask request) via
``rate_classify._classify_pure`` → ``pure_resolvers.resolve_api_key``, and
the MCP router uses ``decorators.resolve_api_key``. A fix that misses the
classifier leaves ``get_request_tier()`` — including the Deribit
gate — trusting the public header while the unit tests go green.
"""

SHARED = 'test-api-key-for-testing'  # forced by tests/conftest.py


# ── Seam 1 (primary): the rate-pool classifier ─────────────────────────────

class _FakeRequest:
    """RequestLike test double (the adapter is an input, not the unit)."""

    def __init__(self, headers=None):
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def header(self, name):
        return self._headers.get(name.lower())

    def query_param(self, name):
        return None

    @property
    def client_ip(self):
        return '203.0.113.9'


class TestClassifierSeam:
    def test_shared_key_on_public_header_is_not_admin(self):
        from carrac.auth.rate_classify import _classify_pure
        ctx = _classify_pure(_FakeRequest({'X-API-Key': SHARED}), dict)
        assert ctx.pool != 'api_key'
        assert ctx.tier != 'admin'

    def test_shared_key_on_internal_header_is_admin(self):
        from carrac.auth.rate_classify import _classify_pure
        ctx = _classify_pure(_FakeRequest({'X-Internal-Key': SHARED}), dict)
        assert ctx.pool == 'api_key'
        assert ctx.tier == 'admin'

    def test_flask_classify_request_stamps_non_admin_for_public_shared(self, app):
        """End-to-end through the real before_request path — the tier every
        ``get_request_tier()`` gate (Deribit included) actually reads."""
        from flask import g
        from carrac.rate_pools import classify_request
        with app.test_request_context(headers={'X-API-Key': SHARED}):
            classify_request()
            assert g.rate_tier != 'admin'
        with app.test_request_context(headers={'X-Internal-Key': SHARED}):
            classify_request()
            assert g.rate_tier == 'admin'


# ── Seam 2: pure_resolvers (framework-neutral, param-driven) ───────────────

class TestPureResolverSeam:
    def test_shared_key_refused_by_default(self, monkeypatch):
        from carrac.auth.pure_resolvers import resolve_api_key
        monkeypatch.setenv('SCREENSHOT_API_KEY', 'secret-shared-key')
        assert resolve_api_key('secret-shared-key') is None

    def test_shared_key_accepted_when_caller_vouches_internal(self, monkeypatch):
        from carrac.auth.pure_resolvers import resolve_api_key
        monkeypatch.setenv('SCREENSHOT_API_KEY', 'secret-shared-key')
        info = resolve_api_key('secret-shared-key', allow_shared=True)
        assert info == {'user_id': None, 'tier': 'admin', 'scope': 'full'}


# ── Seam 3: decorators.resolve_api_key (FastAPI/MCP path) ──────────────────

class TestResolveApiKeySeam:
    def test_shared_key_refused_by_default(self):
        from carrac.auth.decorators import resolve_api_key
        ok, *_ = resolve_api_key(SHARED)
        assert ok is False

    def test_shared_key_accepted_with_allow_shared(self):
        from carrac.auth.decorators import resolve_api_key
        ok, user_id, tier, scope, is_internal = resolve_api_key(
            SHARED, allow_shared=True)
        assert ok is True and user_id is None


# ── Seam 4: Flask verify_api_key / verify_admin_key (fallback path) ────────

class TestFlaskVerifySeam:
    def test_shared_key_needs_the_internal_header(self, app):
        from carrac.auth.decorators import verify_api_key
        with app.test_request_context(headers={'X-Internal-Key': SHARED}):
            assert verify_api_key(SHARED) is True
        with app.test_request_context(headers={'X-API-Key': SHARED}):
            assert verify_api_key(SHARED) is False
        with app.test_request_context():
            assert verify_api_key(SHARED) is False

    def test_shared_key_fails_closed_outside_request_context(self, app):
        """Callable from app-context-only scopes (management commands, two
        existing tests) — must return False, never raise."""
        from carrac.auth.decorators import verify_api_key
        with app.app_context():
            assert verify_api_key(SHARED) is False

    def test_admin_key_same_rule(self, app):
        from carrac.auth.decorators import verify_admin_key
        with app.test_request_context(headers={'X-Internal-Key': SHARED}):
            assert verify_admin_key(SHARED) is True
        with app.test_request_context(headers={'X-API-Key': SHARED}):
            assert verify_admin_key(SHARED) is False
        with app.app_context():
            assert verify_admin_key(SHARED) is False

    def test_per_user_key_untouched_by_the_header_rule(self, app, monkeypatch):
        """cvx_k_ keys keep working on the public header — patch the real
        user_db singleton's lookup (an attribute of the real module, never a
        fabricated module)."""
        import user_db as user_db_module
        from carrac.auth.decorators import verify_api_key
        raw = 'cvx_k_' + 'a' * 32
        monkeypatch.setattr(
            user_db_module.user_db, 'verify_api_key_token',
            lambda h: {'user_id': 42, 'tier': 'member', 'scope': 'full'})
        with app.test_request_context(headers={'X-API-Key': raw}):
            assert verify_api_key(raw) is True


# Seam 5 (the MCP router) lives in tests/fastapi/test_shared_key_header_split.py
# — it needs the asgi_client fixture from tests/fastapi/conftest.py.
