"""Parity tests for @spec-auto-generated OpenAPI paths.

Confirms that every @spec-decorated handler in the live app shows up in the
merged spec, and that all $ref schema references resolve.
"""

import pytest

from carrac.openapi import build_spec, reset_spec_cache
from carrac.openapi.spec_decorator import build_auto_paths


@pytest.fixture(autouse=True)
def _reset_spec_cache():
    reset_spec_cache()
    yield
    reset_spec_cache()


class TestAutoSpecParity:
    def test_at_least_one_decorated_endpoint_exists(self, app):
        """Sanity: ~20 real handlers are decorated."""
        auto = build_auto_paths(app)
        assert len(auto) >= 10, (
            f'Expected ≥10 @spec-decorated paths, got {len(auto)}. '
            f'Paths: {sorted(auto.keys())}'
        )

    def test_key_user_facing_endpoints_are_auto_documented(self, app):
        """The endpoints SDKs need most must appear in the auto spec."""
        auto = build_auto_paths(app)
        # Only paths the Flask app serves carry @spec. Paths FastAPI serves
        # (e.g. /api/batch, /api/scanner, play and alert writes) are
        # described by FastAPI's own OpenAPI document.
        must_exist = {
            '/api/plays',
            '/api/plays/{play_id}',
            '/api/plays/context',
            '/api/alerts',
            '/api/alerts/{alert_id}/toggle',
            '/api/keys',
            '/api/keys/{key_id}',
        }
        missing = must_exist - auto.keys()
        assert not missing, f'Auto-gen missing required paths: {sorted(missing)}'

    def test_fastapi_served_write_paths_are_in_the_document(self, app):
        """The write operations the SDKs call are served by FastAPI, so no
        Flask ``@spec`` describes them; the published document must still
        list each one (and the play backtest, which has no SDK method)."""
        with app.app_context():
            paths = build_spec()['paths']
        expected = {
            ('/api/plays', 'post'),
            ('/api/plays/{play_id}', 'put'),
            ('/api/plays/{play_id}', 'delete'),
            ('/api/plays/{play_id}/backtest', 'post'),
            ('/api/alerts', 'post'),
            ('/api/alerts/{alert_id}', 'put'),
            ('/api/alerts/{alert_id}', 'delete'),
            ('/api/alerts/{alert_id}/toggle', 'post'),
        }
        missing = sorted(p for p in expected if p[1] not in paths.get(p[0], {}))
        assert not missing, f'Operations missing from the OpenAPI document: {missing}'

    def test_api_only_play_and_share_operations_are_documented(self, app):
        """No first-party caller uses these; the document is their description."""
        with app.app_context():
            paths = build_spec()['paths']
        expected = {
            ('/api/plays/{play_id}/link-drawing', 'post'),
            ('/api/plays/{play_id}/unlink-drawing', 'post'),
            ('/api/s/{slug}', 'get'),
        }
        missing = sorted(p for p in expected if p[1] not in paths.get(p[0], {}))
        assert not missing, f'Operations missing from the OpenAPI document: {missing}'

    def test_tier_bars_of_fastapi_served_operations_are_published(self, app):
        """``x-min-tier`` tells SDK users which plan an operation needs."""
        with app.app_context():
            paths = build_spec()['paths']
        expected = {
            ('/api/plays', 'post'): 'member',
            ('/api/plays/{play_id}', 'put'): 'member',
            ('/api/plays/{play_id}', 'delete'): 'member',
            ('/api/plays/{play_id}/backtest', 'post'): 'member',
            ('/api/plays/active', 'get'): 'member',
            ('/api/alerts', 'post'): 'member',
            ('/api/alerts/{alert_id}', 'put'): 'member',
            ('/api/alerts/{alert_id}', 'delete'): 'member',
            ('/api/scanner', 'get'): 'member',
            ('/api/asset/{symbol}/detail', 'get'): 'free',
            ('/api/positions/live', 'get'): 'free',
            ('/api/batch', 'get'): 'member',
        }
        got = {k: paths[k[0]][k[1]].get('x-min-tier') for k in expected}
        assert got == expected

    def test_scanner_documents_its_filters(self, app):
        with app.app_context():
            op = build_spec()['paths']['/api/scanner']['get']
        names = {p['name'] for p in op['parameters']}
        assert {'verdict', 'direction', 'format'} <= names

    def test_merged_spec_includes_auto_paths(self, app):
        """build_spec() must merge auto-gen on top of hand-written paths."""
        with app.app_context():
            spec = build_spec()
        paths = spec['paths']
        # Auto entry should be present.
        assert '/api/keys' in paths
        assert 'get' in paths['/api/keys']
        assert paths['/api/keys']['get']['summary'] == 'List API keys'

    def test_min_tier_extension_emitted(self, app):
        auto = build_auto_paths(app)
        assert auto['/api/plays']['get'].get('x-min-tier') == 'member'

    def test_path_param_typing(self, app):
        auto = build_auto_paths(app)
        play_by_id = auto['/api/plays/{play_id}']['get']
        param_types = {p['name']: p['schema'].get('type') for p in play_by_id['parameters']}
        assert param_types['play_id'] == 'integer'

    def test_all_schema_refs_resolve(self, app):
        """Every $ref in the merged spec must point to a defined schema."""
        with app.app_context():
            spec = build_spec()
        defined = set(spec.get('components', {}).get('schemas', {}).keys())

        refs = []
        def _walk(node, path=''):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == '$ref' and isinstance(v, str):
                        refs.append((v, path))
                    else:
                        _walk(v, f'{path}.{k}')
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    _walk(v, f'{path}[{i}]')

        _walk(spec['paths'])

        broken = []
        for ref, where in refs:
            if not ref.startswith('#/components/schemas/'):
                continue
            name = ref.rsplit('/', 1)[1]
            if name not in defined:
                broken.append((name, where))

        assert not broken, (
            f'{len(broken)} broken $ref(s) in spec. Missing schemas: '
            f'{sorted({n for n, _ in broken})}'
        )

    def test_no_duplicate_operation_ids(self, app):
        """operationId must be unique across the whole spec."""
        with app.app_context():
            spec = build_spec()
        seen = {}
        duplicates = []
        for path, methods in spec['paths'].items():
            for method, op in methods.items():
                if not isinstance(op, dict):
                    continue
                oid = op.get('operationId')
                if not oid:
                    continue
                if oid in seen:
                    duplicates.append((oid, seen[oid], f'{method.upper()} {path}'))
                else:
                    seen[oid] = f'{method.upper()} {path}'
        assert not duplicates, f'Duplicate operationIds: {duplicates}'
