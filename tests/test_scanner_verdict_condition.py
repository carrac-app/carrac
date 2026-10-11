"""The scanner_verdict Plays condition must read the scanner.

The scanner cache (``carrac.caches``)
is the collaborator stubbed here; the evaluator runs through the public
``evaluate_conditions_dict`` entry point.
"""

import pytest

import carrac.caches as caches
from carrac.conditions import evaluate_conditions_dict


def _scanner(btc_long='GOOD', btc_short='WEAK'):
    return {'results': [
        {'symbol': 'BTC', 'price': 77000.0,
         'long': {'total': 8, 'verdict': btc_long},
         'short': {'total': 3, 'verdict': btc_short}},
        {'symbol': 'ETH', 'price': 2100.0,
         'long': {'total': 5, 'verdict': 'NEUTRAL'},
         'short': {'total': 4, 'verdict': 'WEAK'}},
    ]}


def _market(symbol='BTC'):
    # What precompute hands the plays monitor: opportunity present, no verdict key.
    return {'price': 77000.0,
            'asset_data': {'symbol': symbol, 'latest_price': 77000.0,
                           'opportunity': {'score': 61, 'direction': 'long',
                                           'classification': 'B'}},
            'all_assets': {}}


def _rule(direction, min_verdict):
    return {'rules': [{'id': 'r1', 'type': 'scanner_verdict',
                       'params': {'direction': direction, 'min_verdict': min_verdict}}],
            'logic': 'all'}


@pytest.fixture(autouse=True)
def _stub_scanner(monkeypatch):
    monkeypatch.setattr(caches, 'get_cached_scanner', lambda *a, **k: _scanner())


class TestReadsTheScanner:
    def test_long_good_meets_good(self):
        out = evaluate_conditions_dict(_rule('long', 'GOOD'), _market())
        assert out['all_met'] is True, out['rules'][0]['evidence']
        assert 'GOOD' in out['rules'][0]['evidence']

    def test_long_good_does_not_meet_strong(self):
        out = evaluate_conditions_dict(_rule('long', 'STRONG'), _market())
        assert out['all_met'] is False

    def test_direction_selects_the_side(self):
        # BTC short is WEAK: the same threshold that passes for long fails for short.
        assert evaluate_conditions_dict(_rule('short', 'GOOD'), _market())['all_met'] is False
        assert evaluate_conditions_dict(_rule('short', 'WEAK'), _market())['all_met'] is True

    def test_other_symbol(self):
        out = evaluate_conditions_dict(_rule('long', 'NEUTRAL'), _market('ETH'))
        assert out['all_met'] is True


class TestNoScannerData:
    def test_symbol_absent_from_scanner_is_not_met_and_says_so(self):
        out = evaluate_conditions_dict(_rule('long', 'WEAK'), _market('DOGE'))
        assert out['all_met'] is False
        assert 'no scanner data' in out['rules'][0]['evidence'].lower()

    def test_empty_cache_is_not_met_and_says_so(self, monkeypatch):
        monkeypatch.setattr(caches, 'get_cached_scanner', lambda *a, **k: None)
        out = evaluate_conditions_dict(_rule('long', 'WEAK'), _market())
        assert out['all_met'] is False
        assert 'no scanner data' in out['rules'][0]['evidence'].lower()
