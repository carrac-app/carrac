"""The OpenAPI document covers the whole alerts / notification-settings /
drawings surface, and its request bodies are the ones the server validates.

Scope is these three families on purpose: they are what a newcomer needs to
create an alert from the API reference alone (a delivery channel must exist
before an alert can). Other families predate this guard and are not covered
by it.
"""
import re

import pytest

from carrac.fastapi_app.models.alerts import (
    AlertCreateRequest,
    AlertUpdateRequest,
    AnomalyClusterAlertRequest,
    ChainStrengthAlertRequest,
    ConditionalAlertRequest,
    FormulaAlertRequest,
    NotificationSettingsRequest,
    RegimeAlertRequest,
    TrendlineAlertRequest,
    ZscoreAlertRequest,
)
from carrac.fastapi_app.models.drawings import (
    DrawingCreateRequest,
    DrawingDeleteByLocalIdRequest,
    DrawingUpdateRequest,
    DrawingUpsertRequest,
)
from carrac.fastapi_app.routers import alerts as alerts_router
from carrac.fastapi_app.routers import drawings as drawings_router
from carrac.openapi import build_spec, reset_spec_cache

HTTP_METHODS = {'get', 'post', 'put', 'patch', 'delete'}
FAMILIES = ('/api/alerts', '/api/notification-settings', '/api/drawings')

# Served operations in these families that are deliberately not documented.
# Each entry needs a reason; the list is empty because there are none.
NOT_DOCUMENTED: dict[tuple[str, str], str] = {}


@pytest.fixture
def spec_paths(app):
    reset_spec_cache()
    with app.app_context():
        yield build_spec()['paths']
    reset_spec_cache()


def _served(app) -> set[tuple[str, str]]:
    live = set()
    for rule in app.url_map.iter_rules():
        path = re.sub(r'<(?:\w+:)?(\w+)>', r'{\1}', rule.rule)
        live |= {(path, m.lower()) for m in rule.methods if m.lower() in HTTP_METHODS}
    for router in (alerts_router.router, drawings_router.router):
        for route in router.routes:
            live |= {(route.path, m.lower()) for m in route.methods if m.lower() in HTTP_METHODS}
    return {op for op in live if op[0].startswith(FAMILIES)}


def _documented(spec_paths) -> set[tuple[str, str]]:
    return {
        (path, method)
        for path, item in spec_paths.items() if path.startswith(FAMILIES)
        for method in item if method in HTTP_METHODS
    }


def test_every_served_operation_is_documented(app, spec_paths):
    missing = _served(app) - _documented(spec_paths) - set(NOT_DOCUMENTED)
    assert not missing, f'served but not in the OpenAPI document: {sorted(missing)}'


def test_every_documented_operation_is_served(app, spec_paths):
    ghosts = _documented(spec_paths) - _served(app)
    assert not ghosts, f'documented but not served: {sorted(ghosts)}'


def _body(spec_paths, path, method):
    return spec_paths[path][method]['requestBody']['content']['application/json']['schema']


@pytest.mark.parametrize('path,method,model', [
    ('/api/alerts', 'post', AlertCreateRequest),
    ('/api/alerts/{alert_id}', 'put', AlertUpdateRequest),
    ('/api/notification-settings', 'post', NotificationSettingsRequest),
    ('/api/drawings', 'post', DrawingCreateRequest),
    ('/api/drawings/{drawing_id}', 'put', DrawingUpdateRequest),
    ('/api/drawings/upsert', 'post', DrawingUpsertRequest),
    ('/api/drawings/by-local-id', 'delete', DrawingDeleteByLocalIdRequest),
    ('/api/alerts/trendline', 'post', TrendlineAlertRequest),
    ('/api/alerts/formula', 'post', FormulaAlertRequest),
    ('/api/alerts/zscore', 'post', ZscoreAlertRequest),
    ('/api/alerts/regime', 'post', RegimeAlertRequest),
    ('/api/alerts/chain_strength', 'post', ChainStrengthAlertRequest),
    ('/api/alerts/anomaly_cluster', 'post', AnomalyClusterAlertRequest),
    ('/api/alerts/conditional', 'post', ConditionalAlertRequest),
])
def test_request_bodies_list_the_fields_the_server_accepts(spec_paths, path, method, model):
    schema = _body(spec_paths, path, method)
    assert set(schema['properties']) == set(model.model_fields)
    assert schema['additionalProperties'] is False


def test_notification_channel_blocks_match_the_model(spec_paths):
    schema = _body(spec_paths, '/api/notification-settings', 'post')
    for name, field in NotificationSettingsRequest.model_fields.items():
        block = schema['properties'][name]
        assert set(block['properties']) == set(field.annotation.model_fields), name


def test_create_alert_points_at_the_channel_setup(spec_paths):
    description = spec_paths['/api/alerts']['post']['description']
    assert 'delivery channel' in description
    assert 'saveNotificationSettings' in description
    assert spec_paths['/api/notification-settings']['post']['operationId'] == 'saveNotificationSettings'


def test_intro_rate_limit_table_matches_the_limiter(app):
    from carrac.rest_rate_limit import LIMITS_REST

    with app.app_context():
        intro = build_spec()['info']['description']
    for tier in ('free', 'member', 'premium'):
        per_minute, per_hour, per_day = (limit for limit, _seconds, _name in LIMITS_REST[tier])
        row = f'| {tier} | {per_minute:,} | {per_hour:,} | {per_day:,} |'
        assert row in intro, f'intro lacks the {tier} row: {row}'
    assert 'requires Premium' not in intro
