"""Minimal signed Binance REST client for read-only journal sync.

Two endpoints only:
  - GET /sapi/v1/account/apiRestrictions  — permission check on connect.
  - GET /api/v3/myTrades                  — spot account fills, per symbol.

Scope note: v1 syncs SPOT fills only. Binance futures (fapi) fills use a
different endpoint (/fapi/v1/userTrades) with different realized-PnL/fee
semantics (Binance computes realizedPnl itself; funding fees are separate
from trading fees) that don't map cleanly onto the same FIFO builder as spot.
Deliberately out of scope for this client.

All network I/O goes through ``_request`` so tests can monkeypatch it
directly; the hard constraint is that tests NEVER call the
live Binance API.

# WARNING: Binance myTrades without `fromId` returns only the most recent
#          `limit` trades (max 1000) even when startTime/endTime are given —
#          verify current pagination semantics against live Binance docs
#          before relying on this for a user with >1000 fills on one symbol
#          since the last sync. Fine for the 15-minute sync cadence in
#          normal usage; a known open risk.
"""

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import requests

logger = logging.getLogger('carrac.journal.binance_auth')

SPOT_BASE_URL = 'https://api.binance.com'

# Permissions that must ALL be false for a key to be accepted. Binance's
# apiRestrictions response includes several more fields (enableReading,
# ipRestrict, createTime, ...) that are informational only and not checked.
_ELEVATED_PERMISSION_FIELDS = (
    'enableWithdrawals',
    'enableInternalTransfer',
    'enableSpotAndMarginTrading',
    'enableMargin',
    'enableFutures',
    'permitsUniversalTransfer',
)


class BinancePermissionError(Exception):
    """Raised when an API key carries more than read-only permissions."""


class BinanceAuthClient:
    """Signed Binance REST client, scoped to the two endpoints above."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = SPOT_BASE_URL, timeout: float = 15.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.timeout = timeout

    def _signed_get(self, path: str, params: dict = None):
        params = dict(params or {})
        params['timestamp'] = int(time.time() * 1000)
        params.setdefault('recvWindow', 5000)
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256
        ).hexdigest()
        url = f'{self.base_url}{path}?{query}&signature={signature}'
        return self._request(url)

    def _request(self, url: str):
        """The sole network call — the only method tests need to monkeypatch."""
        resp = requests.get(url, headers={'X-MBX-APIKEY': self.api_key}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def check_read_only(self) -> dict:
        """Fetch API key restrictions; raise BinancePermissionError unless
        every elevated permission is disabled. Returns the raw response on
        success (informational fields included)."""
        data = self._signed_get('/sapi/v1/account/apiRestrictions')
        if not isinstance(data, dict):
            raise BinancePermissionError('Unexpected response from apiRestrictions')
        enabled = [f for f in _ELEVATED_PERMISSION_FIELDS if data.get(f)]
        if enabled:
            raise BinancePermissionError(
                'This API key has elevated permissions enabled (' + ', '.join(enabled) + '). '
                'Create a new key with ONLY "Enable Reading" checked and try again.'
            )
        return data

    def get_spot_fills(self, symbol: str, start_time_ms: int = None, limit: int = 1000) -> list:
        """Fetch spot account trades for one symbol.

        ``start_time_ms`` (optional) scopes to fills strictly at/after that
        timestamp (incremental sync cursor). Returns [] on an empty result;
        never raises for "no new fills" (a 4xx from an unknown/delisted
        symbol propagates as an HTTPError — caller handles per-symbol).
        """
        params = {'symbol': symbol, 'limit': max(1, min(int(limit), 1000))}
        if start_time_ms is not None:
            params['startTime'] = int(start_time_ms)
        data = self._signed_get('/api/v3/myTrades', params)
        return data if isinstance(data, list) else []
