"""Tests for carrac.rate_pools — pool classification + Premium API gate."""

import pytest
from flask import Flask, g, session


@pytest.fixture
def mini_app():
    """Minimal Flask app for testing rate pools without full Carrac stack."""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test-secret'
    app.config['SCREENSHOT_API_KEY'] = 'test-api-key-for-testing'
    app.config['TESTING'] = True
    return app


class TestClassifyRequest:
    """Test pool classification for different auth methods."""

    def test_api_key_header(self, mini_app):
        from carrac.rate_pools import classify_request
        with mini_app.test_request_context(
            '/',
            headers={'X-Internal-Key': 'test-api-key-for-testing'},
        ):
            classify_request()
            assert g.rate_pool == 'api_key'
            assert g.rate_key.startswith('api:')
            assert g.rate_tier == 'admin'

    def test_invalid_api_key(self, mini_app):
        from carrac.rate_pools import classify_request
        with mini_app.test_request_context(
            '/',
            headers={'X-API-Key': 'wrong-key'},
        ):
            classify_request()
            # Invalid key should fall through to anonymous
            assert g.rate_pool == 'anonymous'

    def test_widget_token_bearer(self, mini_app):
        from carrac.rate_pools import classify_request
        with mini_app.test_request_context(
            '/',
            headers={'Authorization': 'Bearer cvx_w_abcdef1234567890abcdef1234567890'},
        ):
            classify_request()
            assert g.rate_pool == 'widget_token'
            assert g.rate_key.startswith('widget:')

    def test_widget_token_query_param(self, mini_app):
        from carrac.rate_pools import classify_request
        with mini_app.test_request_context(
            '/?token=cvx_w_abcdef1234567890abcdef1234567890',
        ):
            classify_request()
            assert g.rate_pool == 'widget_token'

    def test_session_user(self, mini_app):
        from carrac.rate_pools import classify_request
        with mini_app.test_request_context('/'):
            session['user_id'] = 42
            classify_request()
            assert g.rate_pool == 'web_session'
            assert g.rate_key == 'web:user:42'

    def test_anonymous(self, mini_app):
        from carrac.rate_pools import classify_request
        with mini_app.test_request_context('/'):
            classify_request()
            assert g.rate_pool == 'anonymous'
            assert g.rate_key.startswith('anon:')

    def test_api_key_takes_priority_over_session(self, mini_app):
        """API key header should be detected even if session exists."""
        from carrac.rate_pools import classify_request
        with mini_app.test_request_context(
            '/',
            headers={'X-Internal-Key': 'test-api-key-for-testing'},
        ):
            session['user_id'] = 42
            classify_request()
            assert g.rate_pool == 'api_key'


class TestApiTierGate:
    """Test the API-key tier allow-list gate (open to all real tiers)."""

    def test_allows_admin_tier(self, mini_app):
        from carrac.rate_pools import check_api_tier_gate
        with mini_app.test_request_context('/'):
            g.rate_pool = 'api_key'
            g.rate_tier = 'admin'
            result = check_api_tier_gate()
            assert result is None  # allowed

    def test_allows_premium_tier(self, mini_app):
        from carrac.rate_pools import check_api_tier_gate
        with mini_app.test_request_context('/'):
            g.rate_pool = 'api_key'
            g.rate_tier = 'premium'
            result = check_api_tier_gate()
            assert result is None

    def test_allows_tester_tier(self, mini_app):
        from carrac.rate_pools import check_api_tier_gate
        with mini_app.test_request_context('/'):
            g.rate_pool = 'api_key'
            g.rate_tier = 'tester'
            result = check_api_tier_gate()
            assert result is None

    def test_allows_member_tier(self, mini_app):
        from carrac.rate_pools import check_api_tier_gate
        with mini_app.test_request_context('/'):
            g.rate_pool = 'api_key'
            g.rate_tier = 'member'
            result = check_api_tier_gate()
            assert result is None

    def test_allows_free_tier(self, mini_app):
        # Free invite-only beta: free users mint + use keys for MCP/TUI.
        from carrac.rate_pools import check_api_tier_gate
        with mini_app.test_request_context('/'):
            g.rate_pool = 'api_key'
            g.rate_tier = 'free'
            result = check_api_tier_gate()
            assert result is None

    def test_skips_non_api_requests(self, mini_app):
        from carrac.rate_pools import check_api_tier_gate
        with mini_app.test_request_context('/'):
            g.rate_pool = 'web_session'
            result = check_api_tier_gate()
            assert result is None  # gate doesn't apply to web sessions

    def test_skips_when_no_pool_set(self, mini_app):
        from carrac.rate_pools import check_api_tier_gate
        with mini_app.test_request_context('/'):
            # g.rate_pool not set (classifier hasn't run)
            result = check_api_tier_gate()
            assert result is None

    def test_blocks_unknown_tier(self, mini_app):
        # A forged/disabled tier outside the allow-list is refused a key.
        from carrac.rate_pools import check_api_tier_gate
        with mini_app.test_request_context('/'):
            g.rate_pool = 'api_key'
            g.rate_tier = 'banana'
            result = check_api_tier_gate()
            assert result is not None
            response, status = result
            assert status == 403
            assert response.get_json()['tier'] == 'banana'


class TestPoolRateKey:
    """Test that the pool-aware rate limit key function works."""

    def test_uses_pool_key_when_set(self, mini_app):
        from carrac.extensions import _rate_limit_key
        with mini_app.test_request_context('/'):
            g.rate_key = 'api:test1234'
            assert _rate_limit_key() == 'api:test1234'

    def test_falls_back_to_user_id(self, mini_app):
        from carrac.extensions import _rate_limit_key
        with mini_app.test_request_context('/'):
            session['user_id'] = 99
            assert _rate_limit_key() == 'user:99'

    def test_falls_back_to_ip(self, mini_app):
        from carrac.extensions import _rate_limit_key
        with mini_app.test_request_context('/', environ_base={'REMOTE_ADDR': '1.2.3.4'}):
            # No g.rate_key, no session
            key = _rate_limit_key()
            assert '1.2.3.4' in key


class TestPoolIsolation:
    """Verify that different pools produce different keys for the same user."""

    def test_web_and_api_keys_differ(self, mini_app):
        from carrac.rate_pools import classify_request
        with mini_app.test_request_context(
            '/',
            headers={'X-Internal-Key': 'test-api-key-for-testing'},
        ):
            classify_request()
            api_key = g.rate_key

        with mini_app.test_request_context('/'):
            session['user_id'] = 42
            classify_request()
            web_key = g.rate_key

        assert api_key != web_key
        assert api_key.startswith('api:')
        assert web_key.startswith('web:')
