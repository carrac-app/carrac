"""One registry rule, applied by every public consumer.

The free /brief is assembled from caches computed over the RS *benchmark* set,
which deliberately includes personal and watchlist symbols. Every
public consumer of those caches has to drop non-registry symbols.

Filtering the RS table alone is not enough: a watchlist symbol stays on the
public page in the Rotation strip, which reads the same cache. So the rule
lives in ONE function and these tests pin that both consumers call it, and
that a third has something to call.
"""
import pytest

from carrac.assets.registry import public_symbol_allowlist


def test_allowlist_is_uppercase_and_populated():
    allowed = public_symbol_allowlist()
    assert allowed, "registry allowlist is empty — every public band would blank"
    assert all(s == s.upper() for s in allowed)
    assert "BTC" in allowed


def test_unavailable_registry_returns_none_not_empty(monkeypatch):
    """The asymmetry that matters: None means 'pass through'. An empty set
    would mean 'drop everything', turning a registry glitch into a blank
    public page — a worse failure than an over-inclusive one."""
    import carrac.assets.registry as reg
    monkeypatch.setattr(reg, "chart_assets", lambda: [])
    assert reg.public_symbol_allowlist() is None

    def _boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr(reg, "chart_assets", _boom)
    assert reg.public_symbol_allowlist() is None


# ── Rotation (the earlier defect) ──────────────────────────────────────

def _rs_rows():
    return [
        {"symbol": "BTC", "quadrant": "leading"},
        {"symbol": "TSLA", "quadrant": "leading"},     # watchlist — must not ship
        {"symbol": "GOLD", "quadrant": "lagging"},
    ]


def _tickers(rows):
    return [a["ticker"] for r in (rows or []) for a in r["assets"]]


def test_rotation_drops_watchlist_symbols():
    """The exact payload that put TSLA on the public brief."""
    from carrac.facts.bands import build_rotation
    tickers = _tickers(build_rotation(rs_data=_rs_rows()))
    assert "TSLA" not in tickers, "watchlist symbol reached the public rotation strip"
    assert "BTC" in tickers and "GOLD" in tickers, "registry assets must survive"


def test_rotation_passes_through_when_registry_unavailable(monkeypatch):
    """Fail open — an unreadable registry must not blank the band."""
    import carrac.facts.bands as bands
    monkeypatch.setattr(bands, "get_cached_rs", lambda: _rs_rows())
    monkeypatch.setattr(
        "carrac.assets.registry.public_symbol_allowlist", lambda: None)
    assert "BTC" in _tickers(bands.build_rotation())


def test_rotation_calls_the_shared_allowlist():
    """Built-but-never-connected is this codebase's most common defect, and
    this test exists because a rule was applied in one consumer only."""
    import inspect
    from carrac.facts import bands
    assert "public_symbol_allowlist" in inspect.getsource(bands.build_rotation)


# ── RS rankings now shares the same primitive ────────────────

def test_rs_filter_uses_the_shared_allowlist():
    import inspect
    from morning_engine.data_assembly import _registry_rs_only
    src = inspect.getsource(_registry_rs_only)
    assert "public_symbol_allowlist" in src
    assert "chart_assets" not in src, "the rule was re-implemented instead of shared"


def test_rs_filter_still_drops_watchlist_symbols():
    from morning_engine.data_assembly import _registry_rs_only
    out = _registry_rs_only({"assets": [{"symbol": "BTC"}, {"symbol": "TSLA"}]})
    assert [a["symbol"] for a in out["assets"]] == ["BTC"]


@pytest.mark.parametrize("bad", [None, {}, {"assets": "nope"}, 42])
def test_rs_filter_never_raises_on_odd_shapes(bad):
    from morning_engine.data_assembly import _registry_rs_only
    assert _registry_rs_only(bad) == bad
