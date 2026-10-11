"""The composite routes enforce the tier bars declared in
carrac/composite/min_tiers.py.

All three paths are served by carrac/fastapi_app/routers/composite.py.
These tests hit the strangler ASGI app, i.e. the exact routing production
traffic takes.
"""

from __future__ import annotations

import pytest

from carrac.composite.min_tiers import (
    ASSET_DETAIL_MIN_TIER,
    BATCH_MIN_TIER,
    LIVE_POSITIONS_MIN_TIER,
)

# (path, params, declared bar).
COMPOSITE_PATHS = [
    ('/api/asset/BTC/detail', {}, ASSET_DETAIL_MIN_TIER),
    ('/api/positions/live', {}, LIVE_POSITIONS_MIN_TIER),
    ('/api/batch', {'assets': 'BTC', 'include': 'market,scanner'}, BATCH_MIN_TIER),
]


class TestBatchTierGate:
    """/api/batch is the concrete leak: a free key could pull
    uncapped scanner scores for 10 symbols per call, bypassing
    FREE_SCANNER_RESULT_LIMIT. Bar = member (aligned with the MCP twin
    util_batch_query, which protocol.py already blocks for free)."""

    def test_free_key_is_refused(self, asgi_client, api_user):
        headers, _uid = api_user('free')
        resp = asgi_client.get('/api/batch',
                               params={'assets': 'BTC,ETH', 'include': 'market,scanner'},
                               headers=headers)
        assert resp.status_code == 403, resp.text
        # exception_handlers.py flattens a dict detail carrying 'error' to
        # the top level — the same wire shape as Flask's tier_required.
        body = resp.json()
        assert body['error'] == 'Subscription required'
        assert body['upgrade_required'] is True
        assert body['current_tier'] == 'free'
        assert 'member' in body['required_tiers']

    def test_member_key_is_served(self, asgi_client, api_user):
        headers, _uid = api_user('member')
        resp = asgi_client.get('/api/batch',
                               params={'assets': 'BTC', 'include': 'market,scanner'},
                               headers=headers)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize('path,params,_bar', COMPOSITE_PATHS)
    def test_anonymous_gets_auth_error_not_tier_error(self, asgi_client, path, params, _bar):
        """Auth runs before the tier gate, so an anonymous caller never sees
        a 403 that would leak tier semantics."""
        resp = asgi_client.get(path, params=params, follow_redirects=False)
        assert resp.status_code in (302, 401), resp.text


class TestFreeBarsStayFree:
    """Requirement pin: enforcement gates only bars above the default tier.
    Asset detail feeds the web details drawer for every logged-in user and
    positions/live returns the caller's own trades — both free, matching
    their MCP twins (market_get_detail / trade_get_positions)."""

    @pytest.mark.parametrize('path,params', [
        ('/api/asset/BTC/detail', {}),
        ('/api/positions/live', {}),
    ])
    def test_free_key_is_served(self, asgi_client, api_user, path, params):
        headers, _uid = api_user('free')
        resp = asgi_client.get(path, params=params, headers=headers)
        assert resp.status_code not in (401, 403), f'{path}: {resp.text}'


class TestGateWiredIntoEveryRoute:
    """Structural proof, not a hand-kept list: every route in the FastAPI
    composite router must resolve its auth through require_min_tier, with
    the bar min_tiers.py declares for that path."""

    def _gates_by_path(self):
        from carrac.fastapi_app.routers import composite as mod

        def _walk(dependant, found):
            for dep in dependant.dependencies:
                call = getattr(dep, 'call', None)
                if call is not None and getattr(call, '_min_tier_gate', None) is not None:
                    found.append(call._min_tier_gate)
                _walk(dep, found)

        gates = {}
        for route in mod.router.routes:
            found: list[str] = []
            _walk(route.dependant, found)
            gates[route.path] = found
        return gates

    def test_every_route_has_exactly_one_gate(self):
        gates = self._gates_by_path()
        assert gates, 'no routes found on the composite router'
        for path, found in gates.items():
            assert len(found) == 1, (
                f'{path}: expected exactly one require_min_tier gate in its '
                f'dependency tree, found {found!r}; a composite route '
                f'must not be ungated'
            )

    def test_gate_bars_match_declared_bars(self):
        """Drift guard: each route's gate uses the bar min_tiers.py declares."""
        declared = {path.replace('BTC', '{symbol}'): bar for path, _p, bar in COMPOSITE_PATHS}
        assert {p: g[0] for p, g in self._gates_by_path().items()} == declared
