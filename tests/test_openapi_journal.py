"""The journal entries of the OpenAPI document match the routes that serve them."""

from __future__ import annotations

import re

import pytest

from carrac.fastapi_app.models.journal import (
    TradeCloseRequest,
    TradeCreateRequest,
    TradeUpdateRequest,
)
from carrac.fastapi_app.routers.journal import router as journal_router
from carrac.openapi import build_spec, reset_spec_cache

HTTP_METHODS = {'get', 'post', 'put', 'patch', 'delete'}


@pytest.fixture
def spec_paths(app):
    reset_spec_cache()
    with app.app_context():
        yield build_spec()['paths']
    reset_spec_cache()


def _live_operations(app) -> set[tuple[str, str]]:
    """(path, method) pairs served by Flask or the FastAPI journal router."""
    live = set()
    for rule in app.url_map.iter_rules():
        path = re.sub(r'<(?:\w+:)?(\w+)>', r'{\1}', rule.rule)
        live |= {(path, m.lower()) for m in rule.methods if m.lower() in HTTP_METHODS}
    for route in journal_router.routes:
        live |= {(route.path, m.lower()) for m in route.methods if m.lower() in HTTP_METHODS}
    return live


def test_every_documented_journal_operation_is_served(app, spec_paths):
    live = _live_operations(app)
    documented = {
        (path, method)
        for path, item in spec_paths.items()
        if path.startswith('/api/journal')
        for method in item if method in HTTP_METHODS
    }
    assert documented, 'no journal operations documented'
    assert not documented - live, f'documented but not served: {sorted(documented - live)}'


def test_trade_lifecycle_is_documented(spec_paths):
    assert 'patch' in spec_paths['/api/journal/trades/{trade_id}']
    assert 'put' not in spec_paths['/api/journal/trades/{trade_id}']
    assert 'post' in spec_paths['/api/journal/trades/{trade_id}/close']


@pytest.mark.parametrize('path,method,model', [
    ('/api/journal/trades', 'post', TradeCreateRequest),
    ('/api/journal/trades/{trade_id}', 'patch', TradeUpdateRequest),
    ('/api/journal/trades/{trade_id}/close', 'post', TradeCloseRequest),
])
def test_request_bodies_come_from_the_validation_models(spec_paths, path, method, model):
    schema = spec_paths[path][method]['requestBody']['content']['application/json']['schema']
    assert set(schema['properties']) == set(model.model_fields)
    assert set(schema.get('required', [])) == {
        name for name, field in model.model_fields.items() if field.is_required()
    }
    assert schema['additionalProperties'] is False


@pytest.mark.parametrize('path,method', [
    ('/api/journal/trades/{trade_id}/reopen', 'post'),
    ('/api/journal/trades/{trade_id}/screenshot', 'post'),
    ('/api/journal/trades/{trade_id}/screenshots', 'get'),
    ('/api/journal/screenshots/{screenshot_id}', 'delete'),
    ('/api/journal/checklists', 'get'),
    ('/api/journal/checklists', 'post'),
    ('/api/journal/checklists/{checklist_id}', 'delete'),
])
def test_api_only_journal_operations_are_documented(spec_paths, path, method):
    """These have no first-party caller, so the document is their only description."""
    assert method in spec_paths[path]


def test_checklist_request_body_comes_from_the_validation_model(spec_paths):
    from carrac.fastapi_app.models.journal import ChecklistRequest

    schema = spec_paths['/api/journal/checklists']['post']['requestBody']['content'][
        'application/json']['schema']
    assert set(schema['properties']) == set(ChecklistRequest.model_fields)
