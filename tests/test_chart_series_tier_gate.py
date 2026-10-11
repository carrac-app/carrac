"""Backend tier gate for GET /api/quant/chart-series.

Sibling of the /api/chart quant gate (tests/test_chart_quant_tier_gate.py, the /api/chart
?quant= gate): the only caller of /api/quant/chart-series is the custom
indicators/formulas feature (static/js/custom-indicators.js, _fetchQuantData
and fetchQuantSeries), whose editor is gated on the frontend behind
window._hasPremiumFeatures = ['premium','admin','tester'].includes(tier) —
i.e. exactly TESTER_OR_HIGHER, NOT member. The route enforces the same tier
server-side, so a free or member user cannot pull quant series off the API.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _client_with_tier(client, tier):
    with client.session_transaction() as sess:
        sess['user_id'] = f'{tier}-user'
        sess['email'] = f'{tier}@example.com'
        sess['is_admin'] = False
        sess['tier'] = tier
    return client


@pytest.fixture
def _series_env(monkeypatch):
    fake_get_aligned_series = MagicMock(
        return_value={"M2": {"values": [1.0, 2.0, 3.0], "label": "M2"}}
    )
    monkeypatch.setattr("quant.bridge.get_aligned_series", fake_get_aligned_series)
    return fake_get_aligned_series


class TestChartSeriesTierGate:
    """?series= must 403 server-side for tiers below premium/tester/admin."""

    @pytest.mark.parametrize("tier", ["free", "member"])
    def test_sub_premium_tier_gets_403(self, client, _series_env, tier):
        _client_with_tier(client, tier)
        resp = client.get(
            "/api/quant/chart-series?series=M2&timestamps=" + json.dumps([1, 2, 3])
        )
        assert resp.status_code == 403, resp.data[:300]
        body = resp.get_json()
        assert body.get("upgrade_required") is True
        _series_env.assert_not_called()

    @pytest.mark.parametrize("tier", ["premium", "tester", "admin"])
    def test_premium_and_above_receives_series(self, client, _series_env, tier):
        _client_with_tier(client, tier)
        resp = client.get(
            "/api/quant/chart-series?series=M2&timestamps=" + json.dumps([1, 2, 3])
        )
        assert resp.status_code == 200, resp.data[:300]
        body = resp.get_json()
        assert body["series"] == {"M2": {"values": [1.0, 2.0, 3.0], "label": "M2"}}
        _series_env.assert_called_once()

    def test_no_session_gets_401_before_tier_check(self, client, _series_env):
        """Unauthenticated callers hit @login_required first (401), never
        reaching the tier gate — same precedence as every other gated route."""
        resp = client.get(
            "/api/quant/chart-series?series=M2&timestamps=" + json.dumps([1, 2, 3])
        )
        assert resp.status_code in (302, 401), resp.status_code
        _series_env.assert_not_called()
