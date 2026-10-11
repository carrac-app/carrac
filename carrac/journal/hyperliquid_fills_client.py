"""Hyperliquid user-fills client for journal auto-sync.

Hyperliquid fills are queryable by wallet ADDRESS alone via the public
``/info`` endpoint — no API key/secret exists on Hyperliquid, so nothing
secret is ever stored for this exchange (see exchange_connections.py).

Reuses the same base-URL convention as the rest of the codebase (the OHLCV
client in data_sources/hyperliquid.py and the smart-money collector in
quant/collectors/hyperliquid_smart_money.py both point at
``https://api.hyperliquid.xyz/info``) rather than hardcoding a second copy.

Endpoint used: POST /info {"type": "userFills", "user": <address>} for a
fresh connection's first sync, or {"type": "userFillsByTime", "user":
<address>, "startTime": <ms>} for incremental syncs. All network I/O goes
through ``_request`` so tests can monkeypatch it directly — never call the
live Hyperliquid API from tests.
"""

import logging

import requests

from data_sources.hyperliquid import HyperliquidClient

logger = logging.getLogger('carrac.journal.hl_fills')

# Reuse the exact base URL the rest of the codebase already uses for HL.
INFO_URL = HyperliquidClient.BASE_URL


class HyperliquidFillsClient:
    """Public /info userFills client — read-only by construction (no auth)."""

    def __init__(self, address: str, timeout: float = 15.0):
        self.address = address
        self.timeout = timeout

    def _request(self, payload: dict):
        """The sole network call — the only method tests need to monkeypatch."""
        resp = requests.post(INFO_URL, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_fills(self, start_time_ms: int = None) -> list:
        """Fetch user fills.

        ``start_time_ms`` (optional): incremental sync cursor. Hyperliquid's
        userFillsByTime is capped at 2000 rows per call and 10k lookback
        days — ample for a 15-minute sync cadence.
        """
        if start_time_ms is not None:
            payload = {
                'type': 'userFillsByTime',
                'user': self.address,
                'startTime': int(start_time_ms),
                'aggregateByTime': False,
            }
        else:
            payload = {'type': 'userFills', 'user': self.address, 'aggregateByTime': False}
        data = self._request(payload)
        return data if isinstance(data, list) else []
