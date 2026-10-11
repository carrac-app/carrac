"""Tests for liquidation spike rows + the Radar half
(imminent macro-event row) on /api/stream.

Liquidation: a separate worker maintains ONE Redis key, 'mcd:liq:summary':
    {"updated_at": <unix s>, "symbols": {"BTC": {"m15_long_usd": float,
     "m15_short_usd": float, "h1_long_usd": float, "h1_short_usd": float,
     "last_ts": <unix s>}, ...}}
get_stream() reads it once per request and applies the spike rule
(carrac.scanner.routes._liquidation_for_symbol). These tests fake that key
directly — no import of the worker.

Event: carrac.scanner.event_risk.get_imminent_event(72) is called from
get_stream() and, if it returns a result, attached as top-level `stream_event`.
"""

import json
import os
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carrac.scanner.routes as routes


class _FakeRedis:
    """Minimal Redis stand-in — only .get() is used by _get_liquidation_summary."""

    def __init__(self, value=None, raise_error=False):
        self._value = value
        self.raise_error = raise_error
        self.calls = []

    def get(self, key):
        self.calls.append(key)
        if self.raise_error:
            raise RuntimeError('redis down (simulated)')
        return self._value


def _liq_raw(symbols, updated_at=None):
    if updated_at is None:
        updated_at = time.time()
    return json.dumps({'updated_at': updated_at, 'symbols': symbols})


def _fake_scanner(entries):
    """`entries` is a list of (symbol, price) tuples."""
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


@pytest.fixture
def member_client(client):
    """Authenticated member-tier client (sees the full stream feed, no preview cap)."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'liq-test-user'
        sess['email'] = 'liq@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'member'
    return client


@pytest.fixture
def free_client(client):
    """Authenticated free-tier client (preview cap applies: FREE_STREAM_ENTRY_LIMIT)."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'liq-free-user'
        sess['email'] = 'liqfree@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'free'
    return client


@pytest.fixture(autouse=True)
def _isolate_watchlist():
    """Disable the watchlist filter so fake symbols always pass through."""
    import user_db as _udb
    with patch.object(_udb.user_db, 'get_flat_watchlist', return_value=[]):
        yield


@pytest.fixture(autouse=True)
def _no_event_by_default():
    """Unless a test overrides it, event_risk must return None (isolates the
    two features from each other — a liquidation test must not accidentally
    also depend on the real quant.events table)."""
    with patch('carrac.scanner.event_risk.get_imminent_event', return_value=None):
        yield


def _get_entry(payload, symbol):
    return next((e for e in payload['entries'] if e['symbol'] == symbol), None)


# ---------------------------------------------------------------------------
# Unit tests for the pure spike-rule helper (fast, no Flask/Redis needed)
# ---------------------------------------------------------------------------

class TestLiquidationForSymbol:
    def test_below_floor_no_spike(self):
        """m15_total under LIQ_SPIKE_MIN_USD never spikes, regardless of ratio."""
        row = {'m15_long_usd': 100000, 'm15_short_usd': 0,
               'h1_long_usd': 0, 'h1_short_usd': 0}
        assert routes._liquidation_for_symbol(row) is None

    def test_at_floor_with_empty_h1_needs_double_floor(self):
        """h1 empty: exactly at the (single) floor is NOT enough — needs 2x."""
        min_usd = routes.LIQ_SPIKE_MIN_USD
        row = {'m15_long_usd': min_usd, 'm15_short_usd': 0,
               'h1_long_usd': 0, 'h1_short_usd': 0}
        assert routes._liquidation_for_symbol(row) is None

    def test_double_floor_with_empty_h1_spikes(self):
        """h1 empty + m15_total >= 2x floor -> spike (absolute fallback)."""
        min_usd = routes.LIQ_SPIKE_MIN_USD
        row = {'m15_long_usd': 2 * min_usd, 'm15_short_usd': 0,
               'h1_long_usd': 0, 'h1_short_usd': 0}
        liq = routes._liquidation_for_symbol(row)
        assert liq is not None
        assert liq['dominant'] == 'long'
        assert liq['total_usd'] == pytest.approx(2 * min_usd)

    def test_ratio_boundary_exactly_3x_trailing_average_spikes(self):
        """h1 present: m15_total == ratio * (h1_total/4) is a spike (>=, not >)."""
        min_usd = routes.LIQ_SPIKE_MIN_USD
        ratio = routes.LIQ_SPIKE_RATIO
        h1_total = 400000.0
        m15_total = ratio * (h1_total / 4.0)
        assert m15_total >= min_usd, 'test fixture must also clear the absolute floor'
        row = {'m15_long_usd': m15_total, 'm15_short_usd': 0,
               'h1_long_usd': h1_total, 'h1_short_usd': 0}
        liq = routes._liquidation_for_symbol(row)
        assert liq is not None

    def test_just_under_ratio_no_spike(self):
        """h1 present: m15_total just below ratio * (h1_total/4) -> no spike."""
        ratio = routes.LIQ_SPIKE_RATIO
        h1_total = 400000.0
        m15_total = ratio * (h1_total / 4.0) - 1
        row = {'m15_long_usd': m15_total, 'm15_short_usd': 0,
               'h1_long_usd': h1_total, 'h1_short_usd': 0}
        assert routes._liquidation_for_symbol(row) is None

    def test_dominant_side_short(self):
        min_usd = routes.LIQ_SPIKE_MIN_USD
        row = {'m15_long_usd': 0, 'm15_short_usd': 2 * min_usd,
               'h1_long_usd': 0, 'h1_short_usd': 0}
        liq = routes._liquidation_for_symbol(row)
        assert liq is not None
        assert liq['dominant'] == 'short'
        assert liq['m15_long_usd'] == 0
        assert liq['m15_short_usd'] == pytest.approx(2 * min_usd)

    def test_malformed_row_returns_none(self):
        assert routes._liquidation_for_symbol({'m15_long_usd': 'not-a-number'}) is None
        assert routes._liquidation_for_symbol({}) is None


# ---------------------------------------------------------------------------
# /api/stream integration tests — liquidation
# ---------------------------------------------------------------------------

class TestStreamLiquidation:
    def test_spike_attached_to_matching_entry(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0)])
        min_usd = routes.LIQ_SPIKE_MIN_USD
        summary = {'BTC': {'m15_long_usd': 2 * min_usd, 'm15_short_usd': 100000,
                            'h1_long_usd': 0, 'h1_short_usd': 0, 'last_ts': time.time()}}
        fake_r = _FakeRedis(_liq_raw(summary))

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=fake_r):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entry = _get_entry(resp.get_json(), 'BTC')
        assert entry is not None
        assert 'liquidation' in entry
        liq = entry['liquidation']
        assert liq['dominant'] == 'long'
        assert liq['m15_long_usd'] == pytest.approx(2 * min_usd)
        assert liq['m15_short_usd'] == pytest.approx(100000)
        assert liq['total_usd'] == pytest.approx(2 * min_usd + 100000)
        # exactly one Redis read for the whole request
        assert fake_r.calls == [routes.LIQ_SUMMARY_REDIS_KEY]

    def test_symbol_below_spike_bar_not_attached(self, member_client):
        scanner = _fake_scanner([('ETH', 50.0)])
        summary = {'ETH': {'m15_long_usd': 1000, 'm15_short_usd': 1000,
                            'h1_long_usd': 0, 'h1_short_usd': 0, 'last_ts': time.time()}}
        fake_r = _FakeRedis(_liq_raw(summary))

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=fake_r):
            resp = member_client.get('/api/stream')

        entry = _get_entry(resp.get_json(), 'ETH')
        assert 'liquidation' not in entry

    def test_symbol_absent_from_summary_not_attached(self, member_client):
        scanner = _fake_scanner([('SOL', 10.0)])
        summary = {'BTC': {'m15_long_usd': 1000000, 'm15_short_usd': 0,
                            'h1_long_usd': 0, 'h1_short_usd': 0, 'last_ts': time.time()}}
        fake_r = _FakeRedis(_liq_raw(summary))

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=fake_r):
            resp = member_client.get('/api/stream')

        entry = _get_entry(resp.get_json(), 'SOL')
        assert 'liquidation' not in entry

    def test_stale_summary_ignored(self, member_client):
        """updated_at older than LIQ_SUMMARY_STALE_AFTER_S -> no liquidation data at all."""
        scanner = _fake_scanner([('BTC', 100.0)])
        min_usd = routes.LIQ_SPIKE_MIN_USD
        stale_ts = time.time() - (routes.LIQ_SUMMARY_STALE_AFTER_S + 30)
        summary = {'BTC': {'m15_long_usd': 5 * min_usd, 'm15_short_usd': 0,
                            'h1_long_usd': 0, 'h1_short_usd': 0, 'last_ts': stale_ts}}
        fake_r = _FakeRedis(_liq_raw(summary, updated_at=stale_ts))

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=fake_r):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entry = _get_entry(resp.get_json(), 'BTC')
        assert 'liquidation' not in entry

    def test_missing_key_keeps_stream_200(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0)])
        fake_r = _FakeRedis(value=None)  # key absent

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=fake_r):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entry = _get_entry(resp.get_json(), 'BTC')
        assert 'liquidation' not in entry

    def test_redis_unavailable_keeps_stream_200(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0)])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=None):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entry = _get_entry(resp.get_json(), 'BTC')
        assert 'liquidation' not in entry

    def test_redis_get_raises_keeps_stream_200(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0)])
        fake_r = _FakeRedis(raise_error=True)

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=fake_r):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entry = _get_entry(resp.get_json(), 'BTC')
        assert 'liquidation' not in entry

    def test_malformed_json_keeps_stream_200(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0)])
        fake_r = _FakeRedis(value='{not valid json')

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=fake_r):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        entry = _get_entry(resp.get_json(), 'BTC')
        assert 'liquidation' not in entry

    def test_free_tier_truncation_applies_automatically(self, free_client):
        """Free tier truncates `entries` to FREE_STREAM_ENTRY_LIMIT BEFORE the
        liquidation attach step — a spike on a truncated-away symbol must not
        appear, with no extra gating logic needed."""
        limit = routes.FREE_STREAM_ENTRY_LIMIT
        syms = [f'SYM{i}' for i in range(limit + 1)]
        scanner = _fake_scanner([(s, 10.0) for s in syms])
        min_usd = routes.LIQ_SPIKE_MIN_USD
        # Put a huge, obvious spike ONLY on the symbol beyond the free-tier cap.
        overflow_sym = syms[limit]
        summary = {overflow_sym: {'m15_long_usd': 10 * min_usd, 'm15_short_usd': 0,
                                   'h1_long_usd': 0, 'h1_short_usd': 0, 'last_ts': time.time()}}
        fake_r = _FakeRedis(_liq_raw(summary))

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.caches._get_redis', return_value=fake_r):
            resp = free_client.get('/api/stream')

        data = resp.get_json()
        assert len(data['entries']) == limit
        assert data.get('preview_mode') is True
        # The overflow symbol never made it into `entries` at all.
        assert _get_entry(data, overflow_sym) is None


# ---------------------------------------------------------------------------
# /api/stream integration tests — imminent event
# ---------------------------------------------------------------------------

class TestStreamEvent:
    def test_event_attached_when_present(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0)])
        fake_event = {'title': 'FOMC Rate Decision', 'ts': '2026-07-08',
                      'hours_until': 48.0, 'impact': 'high'}

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.event_risk.get_imminent_event', return_value=fake_event) as mock_ev:
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['stream_event'] == {
            'title': 'FOMC Rate Decision', 'hours_until': 48.0, 'impact': 'high',
        }
        # Called with the wider 72h feed window, not the scanner's 36h cap.
        mock_ev.assert_called_once_with(72)

    def test_event_none_when_no_imminent_event(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0)])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.event_risk.get_imminent_event', return_value=None):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        assert resp.get_json()['stream_event'] is None

    def test_event_risk_exception_keeps_stream_200(self, member_client):
        scanner = _fake_scanner([('BTC', 100.0)])

        def _boom(hours):
            raise RuntimeError('quant.events unavailable (simulated)')

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch('carrac.scanner.event_risk.get_imminent_event', side_effect=_boom):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['stream_event'] is None
        # Rest of the stream is intact.
        assert len(data['entries']) == 1
        assert data['entries'][0]['symbol'] == 'BTC'

    def test_event_import_error_keeps_stream_200(self, member_client):
        """If the event_risk module itself can't be imported, the stream must
        still respond 200 with stream_event: None (mirrors the try/except in
        get_stream() around the whole `from carrac.scanner.event_risk import
        get_imminent_event` + call)."""
        scanner = _fake_scanner([('BTC', 100.0)])

        with patch('carrac.scanner.routes.get_cached_scanner', return_value=scanner), \
             patch.dict(sys.modules, {'carrac.scanner.event_risk': None}):
            resp = member_client.get('/api/stream')

        assert resp.status_code == 200
        assert resp.get_json()['stream_event'] is None
