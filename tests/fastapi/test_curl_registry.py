import time

import carrac.curl_charts.registry as registry


def test_resolve_known_symbol(monkeypatch):
    monkeypatch.setattr(registry, "_load_symbols", lambda: {"btc", "eth", "spx"})
    registry._invalidate()
    assert registry.resolve("BTC") == "BTC"
    assert registry.resolve("btc") == "BTC"


def test_resolve_unknown_returns_none(monkeypatch):
    monkeypatch.setattr(registry, "_load_symbols", lambda: {"btc"})
    registry._invalidate()
    assert registry.resolve("login") is None
    assert registry.resolve("contact") is None


def test_cache_refreshes_after_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "_load_symbols", lambda: calls.append(1) or {"btc"})
    registry._invalidate()
    registry.resolve("btc")
    registry.resolve("btc")
    assert len(calls) == 1  # cached
    monkeypatch.setattr(registry, "_loaded_at", time.monotonic() - 700)
    registry.resolve("btc")
    assert len(calls) == 2  # refreshed


def test_fail_closed_on_first_load_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(registry, "_load_symbols", boom)
    registry._invalidate()
    assert registry.resolve("BTC") is None


# --- all_symbols tests ---

def test_all_symbols_returns_uppercase_sorted(monkeypatch):
    monkeypatch.setattr(registry, "_load_symbols", lambda: {"eth", "btc", "spx"})
    registry._invalidate()
    result = registry.all_symbols()
    assert result == ["BTC", "ETH", "SPX"]


def test_all_symbols_shares_cache_with_resolve(monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "_load_symbols", lambda: calls.append(1) or {"btc"})
    registry._invalidate()
    registry.resolve("btc")        # loads cache
    registry.all_symbols()         # must NOT reload — shares cached set
    assert len(calls) == 1


def test_all_symbols_empty_on_first_load_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(registry, "_load_symbols", boom)
    registry._invalidate()
    assert registry.all_symbols() == []
