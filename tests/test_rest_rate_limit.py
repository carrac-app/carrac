"""Tests for REST /api/* per-key sliding-window rate limiting."""

import pytest

from carrac.rate_limit import _mem_buckets
from carrac.rest_rate_limit import (
    RateLimitExceeded,
    check_rest_rate_limit,
    peek_rest_rate_limit,
)


@pytest.fixture(autouse=True)
def _clear_mem_buckets(monkeypatch):
    # Force the in-memory backend so these unit tests are hermetic against a
    # locally-running Redis (whose ms-granularity ZSET would otherwise collapse
    # tight-loop calls into the same member and skew counts). Mirrors the MCP
    # rate-limit test fixture.
    monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)
    _mem_buckets.clear()
    yield
    _mem_buckets.clear()


def test_classify_pure_surfaces_api_key_internal_flag(monkeypatch):
    """CARRAC internal-key flag: _classify_pure must set ctx.api_key_internal
    from the resolver's is_internal, for both per-user and shared-key branches."""
    from carrac.auth import rate_classify

    def _fake_resolve(key, *, allow_shared=False):
        return {"user_id": 7, "tier": "member", "scope": "full", "is_internal": True}

    monkeypatch.setattr(
        "carrac.auth.pure_resolvers.resolve_api_key", _fake_resolve, raising=False
    )

    class _Req:
        def header(self, name):
            return "cvx_k_" + "d" * 32 if name == "X-Internal-Key" else None
        def query_param(self, name):
            return None
        @property
        def client_ip(self):
            return "127.0.0.1"

    ctx = rate_classify._classify_pure(_Req(), lambda: {})
    assert ctx.pool == "api_key"
    assert ctx.api_key_internal is True


class TestRestRateLimitMemory:
    def test_admin_unlimited(self):
        # 2000 past premium's minute budget must still pass.
        for _ in range(2000):
            check_rest_rate_limit('admin-rest', 'admin')

    def test_member_minute_limit(self):
        # REST member: 60/min.
        for _ in range(60):
            check_rest_rate_limit('rest-member-1', 'member')
        with pytest.raises(RateLimitExceeded) as exc:
            check_rest_rate_limit('rest-member-1', 'member')
        assert exc.value.limit == 60
        assert exc.value.window_label == 'minute'

    def test_premium_minute_limit(self):
        # REST premium: 120/min.
        for _ in range(120):
            check_rest_rate_limit('rest-prem-1', 'premium')
        with pytest.raises(RateLimitExceeded):
            check_rest_rate_limit('rest-prem-1', 'premium')

    def test_per_key_isolation(self):
        for _ in range(60):
            check_rest_rate_limit('rest-A', 'member')
        # Separate key still has full budget.
        check_rest_rate_limit('rest-B', 'member')

    def test_free_tier_has_budget(self):
        # free is now open (invite beta): 20/min — first 20 succeed, 21st raises.
        for _ in range(20):
            check_rest_rate_limit('rest-free', 'free')
        with pytest.raises(RateLimitExceeded) as exc:
            check_rest_rate_limit('rest-free', 'free')
        assert exc.value.limit == 20

    def test_unknown_tier_treated_as_free(self):
        # Forged/unknown tier degrades to the free budget (least privilege).
        for _ in range(20):
            check_rest_rate_limit('rest-weird', 'carrot')
        with pytest.raises(RateLimitExceeded) as exc:
            check_rest_rate_limit('rest-weird', 'carrot')
        assert exc.value.limit == 20

    def test_mcp_and_rest_counters_are_independent(self):
        """Hit the same key_id on both surfaces; each must keep its own counter."""
        from carrac.mcp.rate_limit import check_rate_limit as mcp_check

        # Fill the MCP minute budget (60 for member).
        for _ in range(60):
            mcp_check('shared-key-1', 'member')
        # REST still has full budget for the same key_id.
        for _ in range(60):
            check_rest_rate_limit('shared-key-1', 'member')
        with pytest.raises(RateLimitExceeded):
            check_rest_rate_limit('shared-key-1', 'member')


class TestPeek:
    def test_peek_does_not_consume(self):
        # Peek 50 times; actual budget remains at 60.
        for _ in range(50):
            peek_rest_rate_limit('peek-1', 'member')
        # 60 real calls must still succeed.
        for _ in range(60):
            check_rest_rate_limit('peek-1', 'member')

    def test_peek_returns_remaining(self):
        for _ in range(10):
            check_rest_rate_limit('peek-2', 'member')
        stats = peek_rest_rate_limit('peek-2', 'member')
        minute = next(u for u in stats['usage'] if u['window'] == 'minute')
        assert minute['used'] == 10
        assert minute['remaining'] == 50
        assert minute['limit'] == 60

    def test_peek_never_raises_when_over_budget(self):
        for _ in range(60):
            check_rest_rate_limit('peek-3', 'member')
        # Already at ceiling — peek must still return (not raise).
        stats = peek_rest_rate_limit('peek-3', 'member')
        minute = next(u for u in stats['usage'] if u['window'] == 'minute')
        assert minute['remaining'] == 0


class TestRestRateLimitHook:
    """Integration: enforce_rest_rate_limit returns 429 JSON on breach."""

    def test_429_on_premium_burst(self, client, monkeypatch):
        """Premium tier: 120/min, then 429 with Retry-After."""
        from flask import g

        from carrac.auth import decorators as auth_dec
        from carrac.auth import pure_resolvers as auth_pure
        from carrac import rate_pools

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 42
            g.api_key_tier = 'premium'
            return True

        def _resolve(key, *, allow_shared=False):
            if not original_verify(key):
                return None
            return {'user_id': 42, 'tier': 'premium', 'scope': 'full'}

        # Patch at every import site so classify_request() picks it up.
        # rate_pools delegates to pure_resolvers.resolve_api_key.
        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(rate_pools, 'verify_api_key', _verify, raising=False)
        monkeypatch.setattr(auth_pure, 'resolve_api_key', _resolve)

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        headers = {'X-Internal-Key': api_key}

        # Hit any /api/* path — the before_request hook runs before routing,
        # so even 404 endpoints exercise the limiter.
        path = '/api/_rate_probe'

        for i in range(120):
            r = client.get(path, headers=headers)
            assert r.status_code != 429, f'unexpected 429 at call {i}'

        r = client.get(path, headers=headers)
        assert r.status_code == 429
        assert r.headers.get('Retry-After')
        data = r.get_json()
        assert data['error'] == 'Rate limit exceeded'
        assert data['window'] == 'minute'
        assert data['limit'] == 120

    def test_admin_burst_never_429(self, client, monkeypatch):
        """Admin tier is unlimited."""
        from flask import g
        from carrac.auth import decorators as auth_dec
        from carrac.auth import pure_resolvers as auth_pure
        from carrac import rate_pools

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 99
            g.api_key_tier = 'admin'
            return True

        def _resolve(key, *, allow_shared=False):
            if not original_verify(key):
                return None
            return {'user_id': 99, 'tier': 'admin', 'scope': 'full'}

        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(rate_pools, 'verify_api_key', _verify, raising=False)
        monkeypatch.setattr(auth_pure, 'resolve_api_key', _resolve)

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        headers = {'X-Internal-Key': api_key}

        for _ in range(500):
            r = client.get('/api/_rate_probe', headers=headers)
            assert r.status_code != 429

    def test_internal_key_never_429_even_at_member_tier(self, client, monkeypatch):
        """CARRAC internal-key flag: an is_internal key on a rate-limited tier
        (member, NOT admin) must never hit the REST 429 — proves the flag,
        not the tier, is what exempts it."""
        from flask import g
        from carrac.auth import decorators as auth_dec
        from carrac.auth import pure_resolvers as auth_pure
        from carrac import rate_pools

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 555
            g.api_key_tier = 'member'
            g.api_key_internal = True
            return True

        def _resolve(key, *, allow_shared=False):
            if not original_verify(key):
                return None
            return {'user_id': 555, 'tier': 'member', 'scope': 'full', 'is_internal': True}

        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(rate_pools, 'verify_api_key', _verify, raising=False)
        monkeypatch.setattr(auth_pure, 'resolve_api_key', _resolve)

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        headers = {'X-Internal-Key': api_key}

        # member is 60/min; 200 calls would 429 a normal member key.
        for i in range(200):
            r = client.get('/api/_rate_probe', headers=headers)
            assert r.status_code != 429, f'unexpected 429 at call {i} for internal key'

    def test_docs_and_spec_not_rate_limited(self, client, monkeypatch):
        """/api/docs and /api/openapi.json must never be rate-limited (public contract)."""
        from flask import g
        from carrac.auth import decorators as auth_dec
        from carrac.auth import pure_resolvers as auth_pure
        from carrac import rate_pools

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 43
            g.api_key_tier = 'member'
            return True

        def _resolve(key, *, allow_shared=False):
            if not original_verify(key):
                return None
            return {'user_id': 43, 'tier': 'member', 'scope': 'full'}

        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(rate_pools, 'verify_api_key', _verify, raising=False)
        monkeypatch.setattr(auth_pure, 'resolve_api_key', _resolve)

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        headers = {'X-Internal-Key': api_key}

        # 200+ calls to the spec must never 429.
        for _ in range(200):
            r = client.get('/api/openapi.json', headers=headers)
            assert r.status_code != 429
