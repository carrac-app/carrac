"""Tests for @spec decorator + auto-generated OpenAPI paths."""

import pytest
from flask import Flask

from carrac.openapi.spec_decorator import spec, build_auto_paths, _flask_rule_to_openapi


# ── Path-rule conversion ───────────────────────────────────────

class TestRuleConversion:
    def test_plain_path(self):
        path, params = _flask_rule_to_openapi('/api/plays')
        assert path == '/api/plays'
        assert params == []

    def test_string_param_default(self):
        path, params = _flask_rule_to_openapi('/api/plays/<play_id>')
        assert path == '/api/plays/{play_id}'
        assert params == [{
            'name': 'play_id', 'in': 'path', 'required': True,
            'schema': {'type': 'string'},
        }]

    def test_int_converter(self):
        path, params = _flask_rule_to_openapi('/api/plays/<int:play_id>')
        assert path == '/api/plays/{play_id}'
        assert params[0]['schema'] == {'type': 'integer'}

    def test_float_converter(self):
        path, params = _flask_rule_to_openapi('/api/x/<float:val>')
        assert params[0]['schema'] == {'type': 'number'}

    def test_multiple_params(self):
        path, params = _flask_rule_to_openapi('/api/chart/<symbol>/<timeframe>')
        assert path == '/api/chart/{symbol}/{timeframe}'
        assert len(params) == 2
        assert params[0]['name'] == 'symbol'
        assert params[1]['name'] == 'timeframe'

    def test_uuid_converter(self):
        path, params = _flask_rule_to_openapi('/api/items/<uuid:item_id>')
        assert params[0]['schema'] == {'type': 'string', 'format': 'uuid'}


# ── Decorator metadata ─────────────────────────────────────────

class TestSpecDecorator:
    def test_stores_metadata_on_function(self):
        @spec(summary='X', tags=['T'])
        def view():
            return 'ok'
        assert view._openapi_spec['summary'] == 'X'
        assert view._openapi_spec['tags'] == ['T']

    def test_wrapping_is_transparent(self):
        @spec(summary='X')
        def view():
            return 'hello'
        assert view() == 'hello'

    def test_default_responses(self):
        @spec()
        def view():
            pass
        # Default is {200: 'OK description'}.
        assert 200 in view._openapi_spec['responses']

    def test_internal_flag_propagates(self):
        @spec(internal=True)
        def view():
            pass
        assert view._openapi_spec['internal'] is True


# ── Auto-gen integration with Flask app ────────────────────────

@pytest.fixture
def mini_app():
    """Tiny Flask app for isolated auto-gen testing."""
    app = Flask(__name__)

    @app.route('/api/hello', methods=['GET'])
    @spec(summary='Hello', tags=['Misc'])
    def hello():
        return 'hi'

    @app.route('/api/items/<int:item_id>', methods=['GET', 'DELETE'])
    @spec(
        summary='Get or delete an item',
        tags=['Items'],
        responses={200: 'Item', 404: 'Error'},
        security=[{'apiKey': []}],
    )
    def item_ops(item_id):
        return {'id': item_id}

    @app.route('/api/create', methods=['POST'])
    @spec(
        summary='Create thing',
        tags=['Things'],
        request_body='CreateThing',
        responses={201: 'Thing'},
    )
    def create_thing():
        return {}

    @app.route('/api/admin/secret', methods=['GET'])
    @spec(summary='Secret admin', internal=True)
    def secret():
        return 'shh'

    @app.route('/api/undocumented', methods=['GET'])
    def undoc():
        return 'no spec'

    return app


class TestBuildAutoPaths:
    def test_collects_decorated_routes(self, mini_app):
        paths = build_auto_paths(mini_app)
        assert '/api/hello' in paths
        assert 'get' in paths['/api/hello']
        assert paths['/api/hello']['get']['summary'] == 'Hello'

    def test_skips_undecorated_routes(self, mini_app):
        paths = build_auto_paths(mini_app)
        assert '/api/undocumented' not in paths

    def test_skips_internal_by_default(self, mini_app):
        paths = build_auto_paths(mini_app)
        assert '/api/admin/secret' not in paths

    def test_include_internal_flag(self, mini_app):
        paths = build_auto_paths(mini_app, include_internal=True)
        assert '/api/admin/secret' in paths

    def test_multiple_methods_per_path(self, mini_app):
        paths = build_auto_paths(mini_app)
        ops = paths['/api/items/{item_id}']
        assert 'get' in ops
        assert 'delete' in ops

    def test_path_params_injected(self, mini_app):
        paths = build_auto_paths(mini_app)
        params = paths['/api/items/{item_id}']['get']['parameters']
        assert any(p['name'] == 'item_id' and p['schema']['type'] == 'integer'
                   for p in params)

    def test_request_body_only_for_mutations(self, mini_app):
        paths = build_auto_paths(mini_app)
        # POST /api/create has a requestBody
        assert 'requestBody' in paths['/api/create']['post']
        # GET /api/hello does not
        assert 'requestBody' not in paths['/api/hello']['get']

    def test_response_schema_refs(self, mini_app):
        paths = build_auto_paths(mini_app)
        get_200 = paths['/api/items/{item_id}']['get']['responses']['200']
        schema = get_200['content']['application/json']['schema']
        assert schema['$ref'] == '#/components/schemas/Item'

    def test_security_passthrough(self, mini_app):
        paths = build_auto_paths(mini_app)
        assert paths['/api/items/{item_id}']['get']['security'] == [{'apiKey': []}]
