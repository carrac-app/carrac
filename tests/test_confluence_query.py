"""Shared confluence cache reader (carrac/scanner/confluence_query)."""

import carrac.caches as caches_mod
from carrac.scanner.confluence_query import get_confluence_map, get_symbol_confluence


def _zone(price, side, label='POC', strength=3.0):
    return {
        'price': price, 'label': label, 'label_friendly': 'Point of Control',
        'sources': ['vp'], 'members': [{'code': 'POC', 'name': 'Point of Control',
        'price': price, 'weight': 3.0, 'family': 'vp'}],
        'strength': strength, 'tier': 'medium', 'side': side, 'count': 1,
        'span': [price, price],
    }


_FAKE = {
    'computed_at': '2026-06-13T00:00:00Z',
    'results': [{
        'symbol': 'BTC', 'price': 100.0,
        'long': {'confluences': {'all': [_zone(90.0, 'support'), _zone(110.0, 'resistance')]}},
        'short': {'confluences': {'all': [_zone(90.0, 'support'), _zone(110.0, 'resistance')]}},
    }],
}


def test_cold_cache(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: None)
    payload, status = get_symbol_confluence('BTC')
    assert status == 'cold' and payload is None


def test_not_found(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: _FAKE)
    payload, status = get_symbol_confluence('DOGE')
    assert status == 'not_found' and payload is None


def test_blank_symbol(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: _FAKE)
    _, status = get_symbol_confluence('')
    assert status == 'not_found'


def test_ok_dedups_across_directions_and_enriches(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: _FAKE)
    payload, status = get_symbol_confluence('btc')   # case-insensitive
    assert status == 'ok'
    assert payload['symbol'] == 'BTC'
    assert payload['price'] == 100.0
    # long+short hold identical zones → deduped to 2 total, not 4
    assert len(payload['all']) == 2
    assert len(payload['supports']) == 1 and len(payload['resistances']) == 1
    z = payload['supports'][0]
    assert z['label_friendly'] == 'Point of Control' and z['tier'] == 'medium'
    assert 'members' in z and 'span' in z


# ── get_confluence_map — fetch the scanner blob ONCE, query many symbols ──────

_FAKE2 = {
    'computed_at': '2026-06-13T00:00:00Z',
    'results': [
        {'symbol': 'BTC', 'price': 100.0,
         'long': {'confluences': {'all': [_zone(90.0, 'support'), _zone(110.0, 'resistance')]}},
         'short': {'confluences': {'all': []}}},
        {'symbol': 'ETH', 'price': 50.0,
         'long': {'confluences': {'all': [_zone(48.0, 'support')]}},
         'short': {'confluences': {'all': []}}},
    ],
}


def test_map_cold_returns_empty(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: None)
    assert get_confluence_map() == {}


def test_map_returns_all_symbols(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: _FAKE2)
    m = get_confluence_map()
    assert set(m) == {'BTC', 'ETH'}
    assert m['BTC']['price'] == 100.0
    assert len(m['BTC']['supports']) == 1 and len(m['BTC']['resistances']) == 1
    assert m['ETH']['supports'][0]['price'] == 48.0


def test_map_filters_by_symbols(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: _FAKE2)
    assert set(get_confluence_map(['btc'])) == {'BTC'}   # case-insensitive


def test_map_fetches_scanner_once(monkeypatch):
    """The whole point: one blob deserialisation for many symbols (was N)."""
    calls = {'n': 0}

    def _counting():
        calls['n'] += 1
        return _FAKE2

    monkeypatch.setattr(caches_mod, 'get_cached_scanner', _counting)
    get_confluence_map(['BTC', 'ETH'])
    assert calls['n'] == 1


def test_map_payload_identical_to_single_reader(monkeypatch):
    monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: _FAKE2)
    single, _ = get_symbol_confluence('BTC')
    assert get_confluence_map(['BTC'])['BTC'] == single
