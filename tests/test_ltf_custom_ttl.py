"""cache_manager: set_cached_ltf_data(ttl=...) / get_cached_ltf_data respect a
custom per-entry TTL, independent of the default.

Extracted from tests/test_cc_consumption.py: this class
tested a generic cache_manager feature — a caller-supplied custom TTL
overriding the default LTF cache TTL — using CryptoCompare's old 4h window as
a convenient example value. The CryptoCompare-specific tests around it
(fallback-symbol tracking, empty-result backoff, the daily rate-limit gate)
were deleted along with the CryptoCompare client itself; this coverage of the
underlying generic mechanism is preserved here under its own name.
"""
from __future__ import annotations

import pandas as pd


def _make_ohlcv(n=5) -> pd.DataFrame:
    """Minimal OHLCV DataFrame with DatetimeIndex."""
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {"open": [1.0] * n, "high": [1.1] * n, "low": [0.9] * n,
         "close": [1.05] * n, "volume": [100.0] * n},
        index=idx,
    )


def _make_ltf_data(n=5) -> dict:
    df = _make_ohlcv(n)
    return {"1h": df, "2h": df.copy(), "4h": df.copy()}


# An arbitrary custom TTL distinct from the module default, used purely to
# prove the override path works — not tied to any specific data source.
_CUSTOM_TTL = 4 * 3600  # 4h


class TestLTFPerEntryTTL:
    """cache_manager: set_cached_ltf_data(ttl=...) / get_cached_ltf_data respect TTL."""

    def test_default_ttl_used_when_not_specified(self, monkeypatch):
        import cache_manager as cm
        fake_r = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: fake_r)
        symbol = "BTC_DTTL"
        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        cm.set_cached_ltf_data(symbol, _make_ltf_data())

        key = f"mcd:ltf:df:{symbol.upper()}"
        assert fake_r.ttl(key) == cm._LTF_CACHE_TTL, "Default TTL must equal _LTF_CACHE_TTL"

        with cm._ltf_cache_lock:
            entry = cm._ltf_cache.get(symbol.upper())
        # Entry is (ts, data, ttl); ttl should be the default
        assert entry is not None
        assert len(entry) == 3
        assert entry[2] == cm._LTF_CACHE_TTL

    def test_custom_ttl_stored_in_l1_and_l2(self, monkeypatch):
        import cache_manager as cm
        fake_r = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: fake_r)
        symbol = "KAS_CTTL"
        custom_ttl = _CUSTOM_TTL

        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        cm.set_cached_ltf_data(symbol, _make_ltf_data(), ttl=custom_ttl)

        # L2
        key = f"mcd:ltf:df:{symbol.upper()}"
        assert fake_r.ttl(key) == custom_ttl, f"L2 TTL {fake_r.ttl(key)} != {custom_ttl}"

        # L1
        with cm._ltf_cache_lock:
            entry = cm._ltf_cache.get(symbol.upper())
        assert entry is not None
        assert entry[2] == custom_ttl, f"L1 TTL {entry[2]} != {custom_ttl}"

    def test_custom_ttl_prevents_early_expiry(self, monkeypatch):
        """Data written with a long custom TTL is still retrievable after _LTF_CACHE_TTL would expire."""
        import cache_manager as cm
        fake_r = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: fake_r)
        symbol = "RNDR_EXPIRY"
        long_ttl = 7200  # 2 h

        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        cm.set_cached_ltf_data(symbol, _make_ltf_data(), ttl=long_ttl)

        # Simulate time passing (past _LTF_CACHE_TTL but inside long_ttl) by
        # back-dating the L1 entry timestamp.
        with cm._ltf_cache_lock:
            ts, data, ttl_stored = cm._ltf_cache[symbol.upper()]
            # Rewind clock by default TTL + 1s  (would expire with default TTL)
            cm._ltf_cache[symbol.upper()] = (ts - (cm._LTF_CACHE_TTL + 1), data, ttl_stored)

        # L1 should still be alive (custom TTL > default TTL)
        result = cm.get_cached_ltf_data(symbol)
        assert result is not None, "Custom TTL entry should still be alive after _LTF_CACHE_TTL seconds"

    def test_standard_symbol_expires_at_default_ttl(self, monkeypatch):
        """Data written without custom TTL expires at _LTF_CACHE_TTL."""
        import cache_manager as cm
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: None)
        symbol = "ETH_STDTTL"

        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        cm.set_cached_ltf_data(symbol, _make_ltf_data())  # no custom TTL

        # Back-date past _LTF_CACHE_TTL
        with cm._ltf_cache_lock:
            ts, data, stored_ttl = cm._ltf_cache[symbol.upper()]
            cm._ltf_cache[symbol.upper()] = (ts - (cm._LTF_CACHE_TTL + 1), data, stored_ttl)

        result = cm.get_cached_ltf_data(symbol)
        assert result is None, "Default-TTL entry should expire after _LTF_CACHE_TTL"


class _FakeRedis:
    """Thread-safe in-memory Redis stub (bytes semantics, honours ex= TTL)."""

    def __init__(self):
        import threading
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, k: str):
        with self._lock:
            return self.store.get(k)

    def set(self, k: str, v, ex: int = None):
        with self._lock:
            self.store[k] = v if isinstance(v, bytes) else v.encode()
            if ex is not None:
                self.ttls[k] = ex

    def delete(self, k: str):
        with self._lock:
            self.store.pop(k, None)
            self.ttls.pop(k, None)

    def ttl(self, k: str) -> int:
        with self._lock:
            return self.ttls.get(k, -1)

    def exists(self, k: str) -> bool:
        with self._lock:
            return k in self.store


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
