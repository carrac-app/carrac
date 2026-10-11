"""Tests for: a user's own drawn levels (horizontal lines /
zones) near price surfaced as Radar (/api/stream) zone rows.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeDrawingDB:
    """Minimal stand-in for carrac.content_db.ContentDatabase.

    Only implements drawing_list(), which is all the stream route touches.
    """

    def __init__(self, drawings=None, raise_error=False):
        self._drawings = drawings if drawings is not None else []
        self.raise_error = raise_error
        self.calls = []

    def drawing_list(self, user_id=None):
        self.calls.append(user_id)
        if self.raise_error:
            raise RuntimeError('drawings backend unavailable (simulated)')
        return self._drawings


def _fake_scanner(entries):
    """Build a scanner payload. `entries` is a list of
    (symbol, price, long_confluences, short_confluences) tuples, where each
    confluences arg is a list of zone dicts (may be empty).
    """
    return {
        'results': [
            {
                'symbol': sym,
                'price': price,
                'best': '',
                'best_score': 0,
                'best_verdict': 'GOOD',
                'long': {'total': 0, 'verdict': 'GOOD', 'confluences': {'all': long_c}},
                'short': {'total': 0, 'verdict': 'AVOID', 'confluences': {'all': short_c}},
            }
            for sym, price, long_c, short_c in entries
        ],
        'macro': {},
        'computed_at': '2026-07-06T00:00:00Z',
    }


def _drawing(asset, price_level, price_level_upper=None, label='',
             drawing_type='horizontal_line'):
    return {
        'asset': asset,
        'drawing_type': drawing_type,
        'price_level': price_level,
        'price_level_upper': price_level_upper,
        'label': label,
    }


@pytest.fixture
def member_client(client):
    """Authenticated member-tier client (sees full stream feed, no preview cap)."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'draw-test-user'
        sess['email'] = 'draw@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'member'
    return client


@pytest.fixture(autouse=True)
def _isolate_watchlist():
    """Disable the watchlist filter so fake symbols always pass through.

    Mirrors tests/test_stream_bias.py — patching the class method is fragile
    across the suite (the route resolves the process-wide user_db.user_db
    instance at call time), so patch the exact instance.
    """
    import user_db as _udb
    with patch.object(_udb.user_db, 'get_flat_watchlist', return_value=[]):
        yield


def _get_zone(entries, symbol, price=None):
    """Find `symbol`'s entry and (optionally) the zone nearest `price`."""
    entry = next(e for e in entries if e['symbol'] == symbol)
    if price is None:
        return entry
    for z in entry['zones']:
        if abs(z['price'] - price) / price <= 0.001:
            return z
    return None


class TestUserDrawingZones:
    def test_near_drawing_appears_as_zone_with_marker(self, member_client):
        """A horizontal line within the 5% window becomes a zone row tagged 'your_level'."""
        scanner = _fake_scanner([('BTC', 100.0, [], [])])
        fake_db = _FakeDrawingDB([_drawing('BTC', 101.0)])  # 1% away

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entries = resp.get_json()['entries']
        zone = _get_zone(entries, 'BTC', 101.0)
        assert zone is not None, 'expected the user level to appear as a zone'
        assert 'your_level' in zone['sources']
        assert zone['side'] == 'resistance'  # level above current price
        assert zone['tf'] == 'D'
        assert zone['strength'] == pytest.approx(1.0)
        assert zone['label'] == 'Your level @101'

        # Exactly one drawings query for the whole request.
        assert len(fake_db.calls) == 1
        assert fake_db.calls[0] == 'draw-test-user'

    def test_far_drawing_excluded(self, member_client):
        """A horizontal line outside the 5% window must not appear at all."""
        scanner = _fake_scanner([('BTC', 100.0, [], [])])
        fake_db = _FakeDrawingDB([_drawing('BTC', 200.0)])  # 100% away

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entries = resp.get_json()['entries']
        entry = _get_zone(entries, 'BTC')
        assert all('your_level' not in z['sources'] for z in entry['zones'])
        assert _get_zone(entries, 'BTC', 200.0) is None

    def test_zone_type_drawing_keeps_band_price_below(self, member_client):
        """A 'zone' drawing keeps its full band; price below → resistance,
        proximity is to the NEAREST (lower) edge."""
        scanner = _fake_scanner([('BTC', 100.0, [], [])])
        fake_db = _FakeDrawingDB([_drawing('BTC', 102.0, price_level_upper=104.0,
                                            drawing_type='zone')])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        entries = resp.get_json()['entries']
        zone = _get_zone(entries, 'BTC', 103.0)  # keyed on midpoint
        assert zone is not None
        assert 'your_level' in zone['sources']
        assert zone['band'] == {'lower': 102.0, 'upper': 104.0}
        assert zone['side'] == 'resistance'

    def test_zone_type_drawing_price_inside_band(self, member_client):
        """Price inside the band → side 'inside', always surfaced."""
        scanner = _fake_scanner([('BTC', 103.0, [], [])])
        fake_db = _FakeDrawingDB([_drawing('BTC', 102.0, price_level_upper=104.0,
                                            drawing_type='zone')])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        entries = resp.get_json()['entries']
        zone = _get_zone(entries, 'BTC', 103.0)
        assert zone is not None
        assert zone['side'] == 'inside'
        assert zone['band'] == {'lower': 102.0, 'upper': 104.0}

    def test_zone_type_drawing_price_above_band(self, member_client):
        """Price above the band → the band acts as support."""
        scanner = _fake_scanner([('BTC', 105.0, [], [])])
        fake_db = _FakeDrawingDB([_drawing('BTC', 102.0, price_level_upper=104.0,
                                            drawing_type='zone')])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        entries = resp.get_json()['entries']
        zone = _get_zone(entries, 'BTC', 103.0)
        assert zone is not None
        assert zone['side'] == 'support'

    def test_zone_type_drawing_far_band_excluded(self, member_client):
        """Band whose NEAREST edge is >5% from price is excluded."""
        scanner = _fake_scanner([('BTC', 100.0, [], [])])
        fake_db = _FakeDrawingDB([_drawing('BTC', 106.0, price_level_upper=110.0,
                                            drawing_type='zone')])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        entries = resp.get_json()['entries']
        assert _get_zone(entries, 'BTC', 108.0) is None

    def test_drawing_own_label_used_when_present(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0, [], [])])
        fake_db = _FakeDrawingDB([_drawing('BTC', 99.0, label='My invalidation')])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        entries = resp.get_json()['entries']
        zone = _get_zone(entries, 'BTC', 99.0)
        assert zone['label'] == 'My invalidation'
        assert zone['side'] == 'support'  # level below current price

    def test_other_drawing_types_ignored(self, member_client):
        """trendline/annotation/scenario drawings must not become zone rows."""
        scanner = _fake_scanner([('BTC', 100.0, [], [])])
        fake_db = _FakeDrawingDB([
            _drawing('BTC', 101.0, drawing_type='trendline'),
            _drawing('BTC', 101.5, drawing_type='annotation'),
            _drawing('BTC', 102.0, drawing_type='scenario'),
        ])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        entries = resp.get_json()['entries']
        entry = _get_zone(entries, 'BTC')
        assert entry['zones'] == []

    def test_merges_into_existing_confluence_zone_within_tolerance(self, member_client):
        """A user level within 0.1% of an existing confluence zone merges into
        it (no duplicate row) instead of appending a new one."""
        conf_zone = {
            'price': 100.05, 'label': 'PDH', 'sources': ['key_levels'],
            'strength': 2.0, 'side': 'resistance', 'count': 1,
        }
        scanner = _fake_scanner([('BTC', 100.0, [], [conf_zone])])
        fake_db = _FakeDrawingDB([_drawing('BTC', 100.06)])  # within 0.1% of 100.05

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        entries = resp.get_json()['entries']
        entry = _get_zone(entries, 'BTC')
        # Only one zone near 100.05 — no duplicate row for the user's level.
        near = [z for z in entry['zones'] if abs(z['price'] - 100.05) / 100.05 <= 0.002]
        assert len(near) == 1
        merged = near[0]
        assert 'key_levels' in merged['sources']
        assert 'your_level' in merged['sources']

        # The original confluence dict's sources list must not be mutated in
        # place (it may be aliased from the shared scanner cache and reused
        # by other users' requests).
        assert conf_zone['sources'] == ['key_levels']

    def test_no_cross_user_leakage_on_shared_confluence_zone(self, member_client, client):
        """Two different users hitting the same (cached) scanner payload must
        not see each other's 'your_level' tags on a shared confluence zone."""
        conf_zone = {
            'price': 100.05, 'label': 'PDH', 'sources': ['key_levels'],
            'strength': 2.0, 'side': 'resistance', 'count': 1,
        }
        scanner = _fake_scanner([('BTC', 100.0, [], [conf_zone])])
        fake_db_with_drawing = _FakeDrawingDB([_drawing('BTC', 100.06)])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db_with_drawing):
            resp1 = member_client.get('/api/stream')
        assert resp1.status_code == 200

        # Second, unrelated user with no drawings of their own, same scanner payload.
        with client.session_transaction() as sess:
            sess['user_id'] = 'other-user'
            sess['email'] = 'other@example.com'
            sess['is_admin'] = False
            sess['tier'] = 'member'
        fake_db_empty = _FakeDrawingDB([])
        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db_empty):
            resp2 = client.get('/api/stream')

        assert resp2.status_code == 200
        entries2 = resp2.get_json()['entries']
        entry2 = _get_zone(entries2, 'BTC')
        near2 = [z for z in entry2['zones'] if abs(z['price'] - 100.05) / 100.05 <= 0.002]
        assert len(near2) == 1
        assert 'your_level' not in near2[0]['sources']

    def test_drawings_backend_failure_keeps_stream_200(self, member_client):
        """A drawings-layer exception must never break the stream response."""
        scanner = _fake_scanner([('BTC', 100.0, [], [])])
        fake_db = _FakeDrawingDB(raise_error=True)

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entries = resp.get_json()['entries']
        assert len(entries) == 1
        assert entries[0]['symbol'] == 'BTC'
        assert entries[0]['zones'] == []

    def test_single_query_across_multiple_watchlist_symbols(self, member_client):
        """One drawings query covers every symbol in the request, not one per symbol."""
        scanner = _fake_scanner([
            ('BTC', 100.0, [], []),
            ('ETH', 50.0, [], []),
            ('SOL', 10.0, [], []),
        ])
        fake_db = _FakeDrawingDB([
            _drawing('BTC', 101.0),
            _drawing('ETH', 50.5),
            _drawing('SOL', 10.1),
        ])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.routes.get_content_db', return_value=fake_db):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entries = resp.get_json()['entries']
        assert len(fake_db.calls) == 1  # one call total, not one per symbol
        for sym, price in (('BTC', 101.0), ('ETH', 50.5), ('SOL', 10.1)):
            zone = _get_zone(entries, sym, price)
            assert zone is not None and 'your_level' in zone['sources']
