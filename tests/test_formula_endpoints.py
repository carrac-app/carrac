"""HTTP surface tests for /api/formula/{lint,preview,evaluate}."""

from unittest.mock import patch

import numpy as np
import pytest


# ── Helpers ────────────────────────────────────────────────────────────

def _fake_ohlcv(n=120):
    return {
        'open':   np.linspace(100, 200, n, dtype=np.float64),
        'high':   np.linspace(101, 201, n, dtype=np.float64),
        'low':    np.linspace(99, 199, n, dtype=np.float64),
        'close':  np.linspace(100.5, 200.5, n, dtype=np.float64),
        'volume': np.full(n, 1000.0, dtype=np.float64),
        '_dates': np.array(['2025-01-01'] * n, dtype='datetime64[ns]'),
    }


@pytest.fixture
def free_client(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 'free-user'
        sess['email'] = 'free@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'free'
    return client


# ── /api/formula/preview (Free tier) ───────────────────────────────────

def test_preview_free_tier_can_access(free_client):
    with patch('carrac.formulas.routes.logger'), \
         patch('carrac.formula_eval.load_ohlcv', return_value=_fake_ohlcv()):
        r = free_client.get('/api/formula/preview?symbol=BTC&tf=1D&expression=rsi(14)')
    assert r.status_code == 200
    data = r.get_json()
    assert data['symbol'] == 'BTC'
    assert 'values' in data
    assert data['engine_version'] >= 3


def test_preview_invalid_timeframe(auth_client):
    r = auth_client.get('/api/formula/preview?symbol=BTC&tf=5m&expression=rsi(14)')
    assert r.status_code == 400
    assert 'timeframe' in r.get_json()['error'].lower()


def test_preview_missing_symbol(auth_client):
    r = auth_client.get('/api/formula/preview?tf=1D&expression=rsi(14)')
    assert r.status_code == 400


def test_preview_bad_expression(auth_client):
    with patch('carrac.formula_eval.load_ohlcv', return_value=_fake_ohlcv()):
        r = auth_client.get('/api/formula/preview?symbol=BTC&tf=1D&expression=not_a_function(1)')
    assert r.status_code == 400
    data = r.get_json()
    assert data['error']


def test_preview_no_data(auth_client):
    with patch('carrac.formula_eval.load_ohlcv', return_value=None):
        r = auth_client.get('/api/formula/preview?symbol=BOGUS&tf=1D&expression=rsi(14)')
    assert r.status_code == 404


def test_preview_requires_auth(client):
    r = client.get('/api/formula/preview?symbol=BTC&tf=1D&expression=rsi(14)')
    assert r.status_code in (302, 401)


# ── PUT /api/custom-formulas/<id> tier gate ───────────────────

class TestCustomFormulaPutTierGate:
    """Free users must not be able to smuggle unlimited custom formulas via
    PUT /api/custom-formulas/<new-id>; the handler enforces the same tier
    check as the POST path."""

    def test_put_rejects_free_tier(self, free_client):
        r = free_client.put('/api/custom-formulas/attack-id',
                            json={'id': 'attack-id', 'name': 'x',
                                  'formula': 'close > 0'})
        assert r.status_code == 403
        data = r.get_json()
        assert data.get('upgrade_required') is True

    def test_put_rejects_over_limit(self, client, monkeypatch):
        """A user at the formula cap cannot use PUT to create a new row."""
        with client.session_transaction() as sess:
            sess['user_id'] = 'premium-user'
            sess['email'] = 'premium@example.com'
            sess['tier'] = 'premium'

        # Patch at the source module — handler imports get_tier_limits
        # lazily from carrac.auth.decorators inside the function body.
        monkeypatch.setattr(
            'carrac.auth.decorators.get_tier_limits',
            lambda _tier: {'custom_formulas': True, 'max_custom_formulas': 3},
        )
        from user_db import user_db as _udb
        monkeypatch.setattr(_udb, 'get_custom_formulas',
                            lambda uid: [{'id': f'f{i}'} for i in range(3)])
        r = client.put('/api/custom-formulas/brand-new-id',
                       json={'id': 'brand-new-id', 'name': 'x',
                             'formula': 'close > 0'})
        assert r.status_code == 403
        data = r.get_json()
        assert data.get('limit_type') == 'custom_formula'
