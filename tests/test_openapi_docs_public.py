"""/api/docs and /api/openapi.json must be publicly accessible."""

import pytest


@pytest.fixture(autouse=True)
def _reset_spec_cache():
    from carrac.openapi import reset_spec_cache
    reset_spec_cache()
    yield
    reset_spec_cache()


class TestDocsArePublic:
    def test_docs_unauthenticated(self, client):
        """Swagger UI page must load without any auth."""
        r = client.get('/api/docs')
        assert r.status_code == 200
        assert b'swagger-ui' in r.data or b'Swagger' in r.data

    def test_openapi_json_unauthenticated(self, client):
        r = client.get('/api/openapi.json')
        assert r.status_code == 200
        data = r.get_json()
        assert 'openapi' in data or 'swagger' in data
        assert 'paths' in data

    def test_openapi_spec_lists_core_endpoints(self, client):
        """Spec should include the SDK-critical routes."""
        r = client.get('/api/openapi.json')
        paths = r.get_json()['paths']
        required = [
            '/api/plays',
            '/api/alerts',
            '/api/asset/{symbol}/detail',
            '/api/batch',
            '/api/scanner',
            '/api/keys',
        ]
        missing = [p for p in required if p not in paths]
        assert not missing, f'OpenAPI spec missing required paths: {missing}'
