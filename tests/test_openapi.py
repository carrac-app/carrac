"""Tests for OpenAPI spec generation, Swagger UI, and API versioning."""

import pytest


# ── Spec structure (no Flask context needed) ──────────────────────

class TestSpecStructure:
    """Validate the OpenAPI spec dict without running Flask."""

    def setup_method(self):
        from carrac.openapi import reset_spec_cache
        reset_spec_cache()

    def test_spec_has_openapi_version(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        assert spec['openapi'] == '3.0.3'

    def test_spec_has_info(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        assert spec['info']['title'] == 'Carrac API'
        assert spec['info']['version'] == '1'

    def test_spec_has_servers(self, monkeypatch):
        from carrac.openapi import build_spec
        monkeypatch.setenv('SITE_URL', 'https://example.test')
        spec = build_spec()
        assert len(spec['servers']) >= 1
        assert spec['servers'][0]['url'] == 'https://example.test'
        assert 'carrac.cc' not in str(spec['servers']) + str(spec['info']['contact'])

    def test_spec_has_all_known_tags(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        tag_names = {t['name'] for t in spec['tags']}
        # Required core tags. New tags can be added
        # without breaking this test.
        required = {'Chart', 'Scanner', 'Plays', 'Alerts', 'Journal',
                    'Sentiment', 'Reports', 'Drawings', 'Quant', 'AI',
                    'Composite'}
        missing = required - tag_names
        assert not missing, f'Missing OpenAPI tags: {missing}'

    def test_spec_has_paths(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        assert len(spec['paths']) >= 30

    def test_spec_has_security_schemes(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        schemes = spec['components']['securitySchemes']
        assert 'api_key' in schemes
        assert 'session_cookie' in schemes
        assert 'widget_token' in schemes

    def test_spec_has_component_schemas(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        schemas = spec['components']['schemas']
        assert 'Error' in schemas
        assert 'Play' in schemas
        assert 'Alert' in schemas
        assert 'Trade' in schemas
        assert 'OHLCV' in schemas
        assert 'Drawing' in schemas
        assert 'ScannerResult' in schemas

    def test_play_schema_has_live_enums(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        play = spec['components']['schemas']['Play']
        directions = play['properties']['direction']['enum']
        assert 'long' in directions
        assert 'short' in directions

    def test_paths_have_operations(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        # Every path should have at least one method
        for path, methods in spec['paths'].items():
            assert len(methods) >= 1, f"{path} has no operations"
            for method, op in methods.items():
                assert 'responses' in op, f"{method.upper()} {path} missing responses"

    def test_spec_caching(self, app):
        from carrac.openapi import build_spec, reset_spec_cache
        reset_spec_cache()
        with app.app_context():
            spec1 = build_spec()
            spec2 = build_spec()
        assert spec1 is spec2  # Same object (cached)

    def test_key_paths_documented(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        paths = spec['paths']
        assert '/api/chart/{symbol}' in paths
        assert '/api/scanner' in paths
        assert '/api/plays' in paths
        assert '/api/alerts' in paths
        assert '/api/journal/trades' in paths
        assert '/api/market-sentiment' in paths
        assert '/api/report/morning/latest' in paths
        assert '/api/drawings' in paths
        assert '/api/quant/health' in paths
        assert '/api/ai/chat' in paths

    def test_ai_endpoints_documented(self):
        """The AI blueprint had ~22 undocumented endpoints.
        Drop 9 added them; lock in so they don't drift away again."""
        from carrac.openapi import build_spec
        spec = build_spec()
        paths = spec['paths']
        expected_ai = {
            '/api/ai/chat', '/api/ai/balance', '/api/ai/assets',
            '/api/ai/conversations', '/api/ai/conversations/{conv_id}',
            '/api/ai/conversations/{conv_id}/messages',
            '/api/ai/quick-score', '/api/ai/context-blocks',
            '/api/ai/context-preview', '/api/ai/execute-action',
            '/api/ai/pattern-match', '/api/ai/journal',
            '/api/ai/journal/asset-history', '/api/ai/journal/analytics',
            '/api/ai/journal/open', '/api/ai/journal/close',
            '/api/ai/journal/{journal_id}', '/api/ai/journal/{journal_id}/reopen',
            '/api/ai/journal/import', '/api/ai/trading-profile',
            '/api/ai/weekly-review', '/api/ai/recipes',
            '/api/ai/recipes/{rid}', '/api/ai/recipes/templates',
        }
        missing = expected_ai - set(paths.keys())
        assert not missing, f'OpenAPI spec is missing AI paths: {sorted(missing)}'


# ── Flask route tests ─────────────────────────────────────────────

class TestOpenAPIRoutes:
    """Test the /api/docs and /api/openapi.json routes."""

    @pytest.fixture(autouse=True)
    def setup_client(self, client):
        self.client = client

    def test_docs_public_after_v0_84(self):
        """/api/docs is public (no auth decorator)."""
        resp = self.client.get('/api/docs')
        assert resp.status_code == 200

    def test_openapi_json_public_after_v0_84(self):
        """/api/openapi.json is public (the spec is the contract)."""
        resp = self.client.get('/api/openapi.json')
        assert resp.status_code == 200


class TestAPIVersionHeader:
    """Test X-API-Version header on all responses."""

    @pytest.fixture(autouse=True)
    def setup_client(self, client):
        self.client = client

    # /health is served by FastAPI (no Flask handler). FastAPI X-API-Version
    # coverage lives at
    # tests/fastapi/test_public_routes.py::TestHealth::test_security_headers.

    def test_version_header_on_static(self):
        resp = self.client.get('/changelog')
        assert resp.headers.get('X-API-Version') == '1'

    def test_version_header_on_api_redirect(self):
        resp = self.client.get('/api/assets')
        assert resp.headers.get('X-API-Version') == '1'


class TestCacheHeaders:
    """Test cache policies for OpenAPI endpoints."""

    @pytest.fixture(autouse=True)
    def setup_client(self, client):
        self.client = client

    def test_openapi_json_cache_policy(self):
        from carrac.cache_headers import get_api_cache_header
        header = get_api_cache_header('/api/openapi.json', 'GET')
        assert 'public' in header
        assert 'max-age=3600' in header

    def test_docs_cache_policy(self):
        from carrac.cache_headers import get_api_cache_header
        header = get_api_cache_header('/api/docs', 'GET')
        assert 'private' in header
