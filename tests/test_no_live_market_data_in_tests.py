"""The suite must never reach a live market-data API.

A ``create_app()`` that populates the price cache synchronously costs one
full Binance 24h ticker book (request weight 80) plus a CoinPaprika tickers
call per test app, ~1,440 of each per run, and Binance weight-bans an IP that
does that. Two layers prevent it: create_app skips the warm-up under TESTING,
and an autouse transport guard refuses any non-loopback HTTP.
"""
import pytest
import requests


@pytest.fixture
def _price_fetch_spy(monkeypatch):
    import cache_manager as cm
    calls = []
    monkeypatch.setattr(cm.CryptoAPIFetcher, 'fetch_combined_prices',
                        staticmethod(lambda: (calls.append('combined'), {})[1]))
    monkeypatch.setattr(cm.CryptoAPIFetcher, 'fetch_binance_tickers',
                        staticmethod(lambda: (calls.append('binance'), {})[1]))
    monkeypatch.setattr(cm.CryptoAPIFetcher, 'fetch_coinpaprika_tickers',
                        staticmethod(lambda: (calls.append('paprika'), {})[1]))
    return calls


def test_create_app_does_not_fetch_live_prices_under_testing(_price_fetch_spy, app):
    # fixture order matters: the spy is installed BEFORE the app fixture runs
    assert _price_fetch_spy == []


def test_transport_guard_refuses_non_loopback_http():
    with pytest.raises(requests.ConnectionError):
        requests.get('https://api.binance.com/api/v3/time', timeout=10)


def test_transport_guard_lets_loopback_through():
    # connection refused on a closed loopback port is a ConnectionError too, so
    # assert on the message: the guard names itself, a refused socket does not.
    with pytest.raises(requests.ConnectionError) as ei:
        requests.get('http://127.0.0.1:9/', timeout=2)
    assert 'Blocked live network call' not in str(ei.value)
