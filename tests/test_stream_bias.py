"""Tests for bias/bias_score fields on /api/stream entries."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_VALID_BIASES = {'long', 'short', 'neutral'}


def _fake_scanner(symbols_and_bests):
    """Build a minimal scanner payload from a list of (symbol, best, best_score) tuples."""
    return {
        'results': [
            {
                'symbol': sym,
                'price': 100.0,
                'best': best,
                'best_score': score,
                'best_verdict': 'GOOD',
                'long': {'total': score if best == 'long' else 0, 'verdict': 'GOOD',
                         'confluences': {'all': []}},
                'short': {'total': score if best == 'short' else 0, 'verdict': 'AVOID',
                          'confluences': {'all': []}},
            }
            for sym, best, score in symbols_and_bests
        ],
        'macro': {},
        'computed_at': '2026-06-13T00:00:00Z',
    }


@pytest.fixture
def member_client(client):
    """Authenticated member-tier client (sees full stream feed)."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'stream-test-user'
        sess['email'] = 'stream@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'member'
    return client


@pytest.fixture
def free_client(client):
    """Free-tier client (sees preview slice)."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'stream-free-user'
        sess['email'] = 'free@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'free'
    return client


@pytest.fixture(autouse=True)
def _isolate_watchlist():
    """Force a deterministically-empty watchlist for every stream test.

    The /api/stream route resolves the ``user_db.user_db`` singleton at call
    time (``from user_db import user_db``) and calls ``.get_flat_watchlist(uid)``.
    Patching the *class* method is fragile in the full suite: an earlier test
    that reloads or replaces the user_db singleton leaves the route holding an
    instance the class-level patch never touches (the user_db reload
    split-brain), so the user's real/seeded watchlist (default ['BTC','ETH','SOL'])
    leaks in and filters out fake symbols like BNB. Patch the exact instance the
    route resolves instead, so the watchlist filter is reliably disabled
    regardless of suite ordering.
    """
    import user_db as _udb
    with patch.object(_udb.user_db, 'get_flat_watchlist', return_value=[]):
        yield


class TestStreamBiasField:
    def test_long_bias_positive_score(self, member_client):
        """best='long' → bias='long', bias_score=+best_score."""
        data = _fake_scanner([('BTC', 'long', 8)])
        with patch('carrac.scanner.routes.get_cached_scanner', return_value=data):
            resp = member_client.get('/api/stream')
        assert resp.status_code == 200
        entries = resp.get_json()['entries']
        assert len(entries) == 1
        e = entries[0]
        assert e['bias'] == 'long'
        assert e['bias_score'] == pytest.approx(8.0)

    def test_short_bias_negative_score(self, member_client):
        """best='short' → bias='short', bias_score=-best_score."""
        data = _fake_scanner([('ETH', 'short', 6)])
        with patch('carrac.scanner.routes.get_cached_scanner', return_value=data):
            resp = member_client.get('/api/stream')
        assert resp.status_code == 200
        entries = resp.get_json()['entries']
        e = entries[0]
        assert e['bias'] == 'short'
        assert e['bias_score'] == pytest.approx(-6.0)

    def test_neutral_when_no_best(self, member_client):
        """Missing best → bias='neutral', bias_score=0.0."""
        data = _fake_scanner([('SOL', '', 0)])
        with patch('carrac.scanner.routes.get_cached_scanner', return_value=data):
            resp = member_client.get('/api/stream')
        assert resp.status_code == 200
        entries = resp.get_json()['entries']
        e = entries[0]
        assert e['bias'] == 'neutral'
        assert e['bias_score'] == pytest.approx(0.0)

    def test_all_entries_have_valid_bias(self, member_client):
        """Every entry must carry bias in {long, short, neutral} and a numeric bias_score.

        The autouse _isolate_watchlist fixture disables the route's watchlist
        filter (empty set → falsy → all symbols pass through), so BNB (not in the
        default seed) survives and we get all 4 entries.
        """
        specs = [('BTC', 'long', 10), ('ETH', 'short', 7), ('SOL', '', 0), ('BNB', 'long', 3)]
        data = _fake_scanner(specs)
        with patch('carrac.scanner.routes.get_cached_scanner', return_value=data):
            resp = member_client.get('/api/stream')
        assert resp.status_code == 200
        entries = resp.get_json()['entries']
        assert len(entries) == 4
        for entry in entries:
            assert 'bias' in entry, f"Missing bias on {entry.get('symbol')}"
            assert entry['bias'] in _VALID_BIASES, f"Invalid bias value: {entry['bias']!r}"
            assert 'bias_score' in entry, f"Missing bias_score on {entry.get('symbol')}"
            assert isinstance(entry['bias_score'], (int, float)), \
                f"bias_score not numeric: {entry['bias_score']!r}"

    def test_free_tier_preview_also_has_bias(self, free_client):
        """Free-tier preview (first 3 entries) must also include bias fields.

        The autouse _isolate_watchlist fixture disables the watchlist filter, so
        all 3 fake symbols pass through and the free-tier cap trims to 3.
        """
        specs = [('BTC', 'long', 5), ('ETH', 'short', 4), ('SOL', 'long', 3)]
        data = _fake_scanner(specs)
        with patch('carrac.scanner.routes.get_cached_scanner', return_value=data):
            resp = free_client.get('/api/stream')
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload.get('preview_mode') is True
        entries = payload['entries']
        # Free tier caps at FREE_STREAM_ENTRY_LIMIT (3); we have exactly 3 symbols
        assert len(entries) == 3
        for entry in entries:
            assert entry['bias'] in _VALID_BIASES
            assert isinstance(entry['bias_score'], (int, float))

    def test_no_advice_words_in_bias_values(self, member_client):
        """bias field must never contain advice words (buy/sell/strong/etc.)."""
        advice_words = {'buy', 'sell', 'strong', 'avoid', 'good', 'watch', 'weak',
                        'bullish', 'bearish', 'recommend'}
        specs = [('BTC', 'long', 9), ('ETH', 'short', 5)]
        data = _fake_scanner(specs)
        with patch('carrac.scanner.routes.get_cached_scanner', return_value=data):
            resp = member_client.get('/api/stream')
        entries = resp.get_json()['entries']
        for entry in entries:
            bias_val = entry['bias'].lower()
            assert bias_val not in advice_words, \
                f"Advice word found in bias: {entry['bias']!r}"
