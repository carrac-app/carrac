"""Backend tier gate for the public /api/chart ``quant`` param.

All 14 macro/quant chart series (M2, Net Liquidity, DXY, Fear & Greed,
Funding, Regime, ...) are ``premiumOnly`` on the frontend — gated behind
``window._hasPremiumFeatures`` (premium/admin/tester tiers, NOT member).
``?quant=`` is checked server-side, like the sibling ``derivatives`` param,
so an authenticated free or member user cannot pull quant series off the API.

Mirrors the Flask-chart mocking pattern from test_chart_macd_param.py (test
env has empty symbol maps, so the db + consensio + indicator calcs are
monkeypatched to deterministic stubs).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


def _make_mock_df(n=120):
    dates = pd.date_range(end="2025-01-01", periods=n, freq="D")
    rng = np.random.default_rng(0)
    close = 30000 + np.cumsum(rng.standard_normal(n) * 100)
    return pd.DataFrame(
        {
            "open": close - 10,
            "high": close + 20,
            "low": close - 20,
            "close": close,
            "volume": rng.uniform(100, 1000, n),
        },
        index=dates,
    )


def _make_mock_db(mock_df, fake_tf_data):
    db = MagicMock()
    db.is_curated.return_value = True
    db.validate_symbol.return_value = None
    db._is_crypto.return_value = True
    db.get_multi_timeframe_data.return_value = fake_tf_data
    db.get_ltf_multi_timeframe_data.return_value = {
        "4h": mock_df, "1h": mock_df, "2h": mock_df,
    }
    db.get_dynamic_ohlcv.return_value = mock_df
    db.binance_map = {"BTC": True}
    db.stock_map = {}
    db.etf_map = {}
    db.hyperliquid = MagicMock()
    db.hyperliquid.is_supported.return_value = False
    db.ccxt = MagicMock()
    db.ccxt.is_supported.return_value = False
    return db


def _make_mock_consensio():
    consensio = MagicMock()
    consensio.calculate_multi_timeframe.return_value = {
        "1D": {"position": 1, "signal": 1},
        "3D": {"position": 0, "signal": 0},
        "1W": {"position": -1, "signal": -1},
        "total": {"positions": 0},
    }
    return consensio


@pytest.fixture
def _chart_env(monkeypatch):
    """Patch the chart route's collaborators with deterministic stubs, and
    stub quant.bridge.get_aligned_series so its call args/return are
    observable without hitting the real quant DB."""
    mock_df = _make_mock_df()
    fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
    mock_db = _make_mock_db(mock_df, fake_tf_data)
    mock_consensio = _make_mock_consensio()
    mock_rsi = MagicMock()
    mock_rsi.calculate_series.return_value = None
    mock_cpr = MagicMock()
    mock_cpr.calculate_monthly_cpr.return_value = None

    monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: None)
    monkeypatch.setattr("carrac.charts.routes._set_cached_chart", lambda *a, **kw: None)
    monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
    monkeypatch.setattr("carrac.charts.routes._get_consensio", lambda: mock_consensio)
    monkeypatch.setattr("carrac.charts.routes._get_rsi_calc", lambda: mock_rsi)
    monkeypatch.setattr("carrac.charts.routes._get_cpr_calc", lambda: mock_cpr)
    monkeypatch.setattr(
        "carrac.charts.routes.compute_session_data", lambda *a, **kw: None, raising=False
    )

    fake_quant = MagicMock(
        return_value={"M2": {"values": [1.0, 2.0, 3.0], "label": "M2", "frequency": "weekly", "axis": "right"}}
    )
    monkeypatch.setattr("quant.bridge.get_aligned_series", fake_quant)
    return fake_quant


def _get_json(resp):
    import json
    return json.loads(resp.data)


def _client_with_tier(client, tier):
    with client.session_transaction() as sess:
        sess['user_id'] = f'{tier}-user'
        sess['email'] = f'{tier}@example.com'
        sess['is_admin'] = False
        sess['tier'] = tier
    return client


class TestChartQuantTierGate:
    """?quant= must be stripped server-side for tiers below premium/tester/admin
    (mirrors the PREMIUM_DERIVATIVES gate immediately above it in routes.py)."""

    @pytest.mark.parametrize("tier", ["free", "member"])
    def test_sub_premium_tier_strips_quant(self, client, _chart_env, tier):
        _client_with_tier(client, tier)
        resp = client.get("/api/chart/BTC?timeframe=1D&quant=M2")
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)
        assert "quant" not in body, f"{tier} tier must not receive quant data"
        _chart_env.assert_not_called()

    @pytest.mark.parametrize("tier", ["premium", "tester", "admin"])
    def test_premium_and_above_receives_quant(self, client, _chart_env, tier):
        _client_with_tier(client, tier)
        resp = client.get("/api/chart/BTC?timeframe=1D&quant=M2")
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)
        assert "quant" in body, f"{tier} tier should receive quant data"
        assert body["quant"] == {"M2": {"values": [1.0, 2.0, 3.0], "label": "M2", "frequency": "weekly", "axis": "right"}}
        _chart_env.assert_called_once()
        called_series = _chart_env.call_args[0][0]
        assert called_series == ["M2"]

    def test_no_quant_param_omits_quant_key(self, client, _chart_env):
        """Pins that no ?quant means no quant key, regardless of tier."""
        _client_with_tier(client, "premium")
        resp = client.get("/api/chart/BTC?timeframe=1D")
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)
        assert "quant" not in body
        _chart_env.assert_not_called()
