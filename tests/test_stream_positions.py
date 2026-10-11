"""Tests for: a user's own open positions surfaced as
Radar (/api/stream) `positions` rows.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_scanner(entries=()):
    """Build a minimal scanner payload. `entries` is a list of (symbol, price)."""
    return {
        'results': [
            {
                'symbol': sym,
                'price': price,
                'best': '',
                'best_score': 0,
                'best_verdict': 'GOOD',
                'long': {'total': 0, 'verdict': 'GOOD', 'confluences': {'all': []}},
                'short': {'total': 0, 'verdict': 'AVOID', 'confluences': {'all': []}},
            }
            for sym, price in entries
        ],
        'macro': {},
        'computed_at': '2026-07-06T00:00:00Z',
    }


def _fake_position(asset='SOL', direction='long', entry_price=150.0,
                    current_price=152.0, stop_price=145.0, target_price=170.0,
                    unrealized_pnl_pct=1.33, current_rr=0.4, risk_status='neutral',
                    time_in_trade='2d 3h'):
    """A single enriched position, shaped like assemble_live_positions()'s output."""
    return {
        'id': 1, 'asset': asset, 'direction': direction,
        'entry_price': entry_price, 'current_price': current_price,
        'stop_price': stop_price, 'target_price': target_price,
        'unrealized_pnl_pct': unrealized_pnl_pct, 'current_rr': current_rr,
        'risk_status': risk_status, 'scanner_verdict': 'GOOD', 'linked_play': None,
        'time_in_trade': time_in_trade, 'setup_type': None, 'opened_at': '2026-07-04T00:00:00Z',
        'mfe_r': None, 'mae_r': None, 'mfe_price': None, 'mae_price': None,
        'mfe_computed_at': None,
    }


@pytest.fixture
def member_client(client):
    """Authenticated member-tier client (sees the full stream feed, no preview cap)."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'pos-test-user'
        sess['email'] = 'pos@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'member'
    return client


@pytest.fixture(autouse=True)
def _isolate_watchlist():
    """Disable the watchlist filter so fake symbols always pass through.

    Mirrors tests/test_stream_user_drawings.py — patching the class method is
    fragile across the suite, so patch the exact process-wide instance.
    """
    import user_db as _udb
    with patch.object(_udb.user_db, 'get_flat_watchlist', return_value=[]):
        yield


def _get_position(payload, symbol):
    return next((p for p in payload.get('positions', []) if p.get('symbol') == symbol), None)


class TestStreamPositions:
    def test_position_appears_with_computed_fields(self, member_client):
        """An open long trade shows up in `positions` with entry/pnl/rr/stop_distance."""
        scanner = _fake_scanner([('SOL', 152.0)])
        fake_positions = {'positions': [_fake_position()], 'portfolio_heat': {}, 'availability': {}}

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.composite.assemblers.assemble_live_positions', return_value=fake_positions):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'positions' in data
        pos = _get_position(data, 'SOL')
        assert pos is not None
        assert pos['direction'] == 'long'
        assert pos['risk_status'] == 'neutral'
        assert pos['unrealized_pnl_pct'] == pytest.approx(1.33)
        assert pos['current_rr'] == pytest.approx(0.4)
        assert pos['entry_price'] == pytest.approx(150.0)
        assert pos['age'] == '2d 3h'
        # stop_distance_pct = (current - stop) / current * 100 for a long
        assert pos['stop_distance_pct'] == pytest.approx(round((152.0 - 145.0) / 152.0 * 100, 2))

    def test_stop_distance_pct_for_short(self, member_client):
        """Short direction flips the stop-distance sign convention."""
        scanner = _fake_scanner([('ETH', 95.0)])
        short_pos = _fake_position(asset='ETH', direction='short', entry_price=100.0,
                                    current_price=95.0, stop_price=105.0,
                                    unrealized_pnl_pct=5.0, current_rr=1.0)
        fake_positions = {'positions': [short_pos], 'portfolio_heat': {}, 'availability': {}}

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.composite.assemblers.assemble_live_positions', return_value=fake_positions):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        pos = _get_position(resp.get_json(), 'ETH')
        assert pos['direction'] == 'short'
        # stop_distance_pct = (stop - current) / current * 100 for a short
        assert pos['stop_distance_pct'] == pytest.approx(round((105.0 - 95.0) / 95.0 * 100, 2))

    def test_stopped_out_position_has_negative_stop_distance(self, member_client):
        """A breached stop yields a negative stop_distance_pct (past the stop)."""
        scanner = _fake_scanner([('BTC', 140.0)])
        blown = _fake_position(asset='BTC', direction='long', entry_price=150.0,
                                current_price=140.0, stop_price=145.0,
                                unrealized_pnl_pct=-6.67, current_rr=-1.0,
                                risk_status='stopped_out')
        fake_positions = {'positions': [blown], 'portfolio_heat': {}, 'availability': {}}

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.composite.assemblers.assemble_live_positions', return_value=fake_positions):
            resp = member_client.get('/api/stream')

        pos = _get_position(resp.get_json(), 'BTC')
        assert pos['risk_status'] == 'stopped_out'
        assert pos['stop_distance_pct'] < 0

    def test_position_not_restricted_to_watchlist_symbols(self, member_client):
        """A held symbol absent from the scanner/watchlist universe still surfaces."""
        scanner = _fake_scanner([])  # no scanner entries at all — not on any watchlist
        fake_positions = {'positions': [_fake_position(asset='DOGE')], 'portfolio_heat': {}, 'availability': {}}

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.composite.assemblers.assemble_live_positions', return_value=fake_positions):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['entries'] == []  # nothing watchlisted
        assert _get_position(data, 'DOGE') is not None  # but the position still shows

    def test_no_open_positions_returns_empty_list(self, member_client):
        """No open trades → positions: [] (not omitted, not null)."""
        scanner = _fake_scanner([('BTC', 100.0)])
        empty = {'positions': [], 'portfolio_heat': {'total_exposure': 0, 'long_count': 0, 'short_count': 0}, 'availability': {}}

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.composite.assemblers.assemble_live_positions', return_value=empty):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        assert resp.get_json()['positions'] == []

    def test_assembler_exception_keeps_stream_200(self, member_client):
        """A positions-assembler failure must never break the stream response."""
        scanner = _fake_scanner([('BTC', 100.0)])

        def _boom(user_id, **kw):
            raise RuntimeError('positions backend unavailable (simulated)')

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.composite.assemblers.assemble_live_positions', side_effect=_boom):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['positions'] == []
        # The rest of the stream must still be intact.
        assert len(data['entries']) == 1
        assert data['entries'][0]['symbol'] == 'BTC'
