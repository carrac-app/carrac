"""Hyperliquid-owned coins must never be served from Binance.

Binance still answers for a few of these tickers with a different, delisted
asset (LITUSDT is Litentry, status BREAK, last bar months old). Served first,
that dead series made a fresh install raise a delisting alarm for a coin that
trades normally on Hyperliquid.
"""
from unittest.mock import MagicMock

import pandas as pd

from database import OHLCVDatabase


def _frame(tag):
    return pd.DataFrame({'close': [1.0]}, index=pd.to_datetime(['2025-01-01'])).assign(src=tag)


def _db(hl_supported, hl_df):
    db = OHLCVDatabase.__new__(OHLCVDatabase)
    db.binance = MagicMock()
    db.hyperliquid = MagicMock()
    db.ccxt = MagicMock()
    db.binance.fetch_ohlcv.return_value = _frame('binance')
    db.binance.fetch_hourly_ohlcv.return_value = _frame('binance')
    db.hyperliquid.is_supported.return_value = hl_supported
    db.hyperliquid.fetch_ohlcv.return_value = hl_df
    db.hyperliquid.fetch_hourly_ohlcv.return_value = hl_df
    db.ccxt.is_supported.return_value = False
    return db


def test_daily_hl_owned_coin_is_read_from_hyperliquid_first():
    db = _db(True, _frame('hl'))
    assert db._fetch_crypto_data('LIT', 30)['src'].iloc[0] == 'hl'
    db.binance.fetch_ohlcv.assert_not_called()


def test_hourly_hl_owned_coin_is_read_from_hyperliquid_first():
    db = _db(True, _frame('hl'))
    assert db._fetch_hourly_data('LIT', 30)['src'].iloc[0] == 'hl'
    db.binance.fetch_hourly_ohlcv.assert_not_called()


def test_hl_outage_does_not_fall_back_to_the_wrong_binance_series():
    db = _db(True, pd.DataFrame())
    assert db._fetch_crypto_data('LIT', 30).empty
    db.binance.fetch_ohlcv.assert_not_called()


def test_other_coins_still_try_binance_first():
    db = _db(False, pd.DataFrame())
    assert db._fetch_crypto_data('ETH', 30)['src'].iloc[0] == 'binance'
    db.hyperliquid.fetch_ohlcv.assert_not_called()
