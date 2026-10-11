"""
Tests for carrac.auth.decorators — authentication and authorization.
"""



class TestVerifyApiKey:
    def test_valid_key(self, app):
        with app.test_request_context(headers={'X-Internal-Key': 'test-api-key-for-testing'}):
            from carrac.auth.decorators import verify_api_key
            assert verify_api_key('test-api-key-for-testing') is True

    def test_invalid_key(self, app):
        with app.app_context():
            from carrac.auth.decorators import verify_api_key
            assert verify_api_key('wrong-key') is False

    def test_empty_key(self, app):
        with app.app_context():
            from carrac.auth.decorators import verify_api_key
            assert verify_api_key('') is False
            assert verify_api_key(None) is False


class TestLoginRequired:
    def test_unauthenticated_redirects(self, client):
        """Protected API returns 302 or 401 without auth."""
        response = client.get('/api/watchlist')
        assert response.status_code in [302, 401]

    def test_authenticated_passes(self, auth_client):
        """Protected API returns 200 with auth."""
        response = auth_client.get('/api/watchlist')
        assert response.status_code == 200


class TestAuthOrKeyRequired:
    def test_no_auth_rejects(self, client):
        response = client.get('/api/market-sentiment')
        assert response.status_code in [302, 401]

    def test_session_auth_passes(self, auth_client):
        response = auth_client.get('/api/market-sentiment')
        assert response.status_code == 200
        assert 'sentiment' in response.get_json()

    def test_api_key_passes(self, client):
        response = client.get('/api/market-sentiment',
                              headers={'X-Internal-Key': 'test-api-key-for-testing'})
        assert response.status_code == 200
        assert 'sentiment' in response.get_json()

    def test_header_key_passes(self, client):
        response = client.get('/api/market-sentiment',
                              headers={'X-Internal-Key': 'test-api-key-for-testing'})
        assert response.status_code == 200
        assert 'sentiment' in response.get_json()

    def test_bad_key_in_url_rejected(self, client):
        """API key in URL params is rejected (security: use X-API-Key header)."""
        response = client.get('/api/market-sentiment?key=wrong',
                              headers={'Accept': 'application/json'})
        assert response.status_code == 400

    def test_bad_key_in_header_rejects(self, client):
        response = client.get('/api/market-sentiment',
                              headers={'X-API-Key': 'wrong-key',
                                       'Accept': 'application/json'})
        assert response.status_code in [401, 302]


# Admin-required HTTP coverage for /api/security-status lives in:
#   tests/fastapi/test_admin_billing_json.py::test_security_status_requires_admin
#   tests/fastapi/test_auth.py::TestRequireAdmin (5 tests covering positive
#   admin pass, non-admin 403, DB inactive redirect, DB error 503, and the
#   no-session redirect path).
