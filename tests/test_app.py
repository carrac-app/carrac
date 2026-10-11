"""
Tests for the Flask application routes and integration.

Uses the application factory via conftest fixtures (app, client, auth_client).
"""

import pytest


class TestApiAuth:
    """Test API authentication patterns using /api/market-sentiment."""

    def test_api_requires_auth(self, client):
        response = client.get('/api/market-sentiment')
        assert response.status_code in [302, 401]

    def test_api_with_api_key(self, client):
        response = client.get('/api/market-sentiment',
                              headers={'X-Internal-Key': 'test-api-key-for-testing'})
        assert response.status_code == 200
        assert 'sentiment' in response.get_json()

    def test_api_with_bad_key_in_url(self, client):
        """API key in URL params is rejected (security: use X-API-Key header)."""
        response = client.get('/api/market-sentiment?key=wrong-key',
                              headers={'Accept': 'application/json'})
        assert response.status_code == 400

    def test_api_with_bad_key_in_header(self, client):
        response = client.get('/api/market-sentiment',
                              headers={'X-API-Key': 'wrong-key',
                                       'Accept': 'application/json'})
        assert response.status_code in [401, 302]

    def test_api_with_session(self, auth_client):
        response = auth_client.get('/api/market-sentiment')
        assert response.status_code == 200
        assert 'sentiment' in response.get_json()


class TestChartApi:
    """Test chart data API."""

    def test_chart_requires_auth(self, client):
        response = client.get('/api/chart/BTC')
        assert response.status_code == 302


class TestErrorHandling:
    """Test error handling."""

    def test_404_error(self, client):
        response = client.get('/nonexistent-route-xyz')
        assert response.status_code == 404

    def test_method_not_allowed(self, client):
        response = client.delete('/login')
        assert response.status_code == 405


class TestStaticFiles:
    """Test static file serving."""

    def test_static_css(self, client):
        response = client.get('/static/css/main.css')
        assert response.status_code == 200

    def test_static_js(self, client):
        response = client.get('/static/js/main.js')
        assert response.status_code == 200


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
