"""Tests for /api/usage and usage counters."""

import pytest

from carrac.rate_limit import _mem_buckets
from carrac.usage import build_usage_response, record_usage, read_history


@pytest.fixture(autouse=True)
def _reset_rate_state():
    from carrac.openapi import reset_spec_cache
    _mem_buckets.clear()
    reset_spec_cache()
    yield
    _mem_buckets.clear()
    reset_spec_cache()


class TestBuildUsageResponse:
    def test_returns_rest_and_mcp_sections(self):
        r = build_usage_response(user_id=1, tier='premium', include_history=False)
        assert 'rest' in r
        assert 'mcp' in r
        assert r['tier'] == 'premium'
        assert r['user_id'] == 1

    def test_admin_is_unlimited(self):
        r = build_usage_response(user_id=2, tier='admin', include_history=False)
        assert r['rest']['unlimited'] is True
        assert r['mcp']['unlimited'] is True

    def test_member_peek_does_not_consume(self):
        # Call 3 times, peek should show 0 consumed (peek doesn't record).
        for _ in range(3):
            build_usage_response(user_id=99, tier='premium', include_history=False)
        r = build_usage_response(user_id=99, tier='premium', include_history=False)
        minute = next(u for u in r['rest']['usage'] if u['window'] == 'minute')
        assert minute['used'] == 0

    def test_include_history_flag(self):
        with_history = build_usage_response(user_id=3, tier='premium', include_history=True, history_days=5)
        assert 'history' in with_history
        # With Redis up, history has 5 rows; without Redis, it's []. Both acceptable.
        assert isinstance(with_history['history'], list)

        without = build_usage_response(user_id=3, tier='premium', include_history=False)
        assert 'history' not in without


class TestUsageEndpoint:
    """Integration: GET /api/usage returns JSON usage payload."""

    def test_unauth_returns_401_or_redirect(self, client):
        r = client.get('/api/usage')
        # @auth_or_key_required → 401 for JSON callers, 302 for browsers
        assert r.status_code in (302, 401)

    def test_api_key_caller_gets_payload(self, client, monkeypatch):
        from flask import g
        from carrac.auth import decorators as auth_dec
        from carrac.auth import pure_resolvers as auth_pure
        from carrac import rate_pools

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 77
            g.api_key_tier = 'premium'
            return True

        def _resolve(key, *, allow_shared=False):
            if not original_verify(key):
                return None
            return {'user_id': 77, 'tier': 'premium', 'scope': 'full'}

        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(rate_pools, 'verify_api_key', _verify, raising=False)
        monkeypatch.setattr(auth_pure, 'resolve_api_key', _resolve)

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        r = client.get('/api/usage', headers={'X-Internal-Key': api_key, 'Accept': 'application/json'})
        assert r.status_code == 200
        data = r.get_json()
        assert data['user_id'] == 77
        assert data['tier'] == 'premium'
        assert 'rest' in data
        assert 'mcp' in data

    def test_history_param_honored(self, client, monkeypatch):
        from flask import g
        from carrac.auth import decorators as auth_dec
        from carrac.auth import pure_resolvers as auth_pure
        from carrac import rate_pools

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 78
            g.api_key_tier = 'premium'
            return True

        def _resolve(key, *, allow_shared=False):
            if not original_verify(key):
                return None
            return {'user_id': 78, 'tier': 'premium', 'scope': 'full'}

        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(rate_pools, 'verify_api_key', _verify, raising=False)
        monkeypatch.setattr(auth_pure, 'resolve_api_key', _resolve)

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        r = client.get('/api/usage?history=false',
                       headers={'X-Internal-Key': api_key, 'Accept': 'application/json'})
        data = r.get_json()
        assert 'history' not in data

    def test_usage_endpoint_is_in_openapi_spec(self, client):
        r = client.get('/api/openapi.json')
        paths = r.get_json()['paths']
        assert '/api/usage' in paths
        assert 'get' in paths['/api/usage']


class TestUsageCounters:
    def test_record_usage_no_user_is_noop(self):
        # Should not raise even though Redis might be down.
        record_usage(None)
        record_usage(0)

    def test_read_history_returns_list_of_days(self):
        hist = read_history(user_id=111, days=7)
        # May be empty (no Redis in tests) but must be a list of the right length or empty.
        assert isinstance(hist, list)
        # If Redis is up, length == 7. If not, length == 0. Both are acceptable.
        assert len(hist) in (0, 7)


class TestUsageCounterInternalKeyExclusion:
    def test_track_api_request_skips_internal_key(self, app, monkeypatch):
        """CARRAC internal-key flag: mcd:usage: daily rollup must not
        increment for a request classified as an internal API key."""
        from flask import g
        import carrac.usage as usage_mod

        calls = []
        monkeypatch.setattr(
            usage_mod, 'record_usage',
            lambda uid, surface='rest': calls.append((uid, surface)),
        )

        with app.test_request_context('/api/plays'):
            g.api_key_user_id = 555
            g.api_key_internal = True
            usage_mod.track_api_request(app.response_class())

        assert calls == []

    def test_track_api_request_records_non_internal_key(self, app, monkeypatch):
        """Control: a normal (non-internal) API key still increments the counter."""
        from flask import g
        import carrac.usage as usage_mod

        calls = []
        monkeypatch.setattr(
            usage_mod, 'record_usage',
            lambda uid, surface='rest': calls.append((uid, surface)),
        )

        with app.test_request_context('/api/plays'):
            g.api_key_user_id = 556
            usage_mod.track_api_request(app.response_class())

        assert calls == [(556, 'rest')]
