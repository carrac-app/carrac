from carrac.reports.pulse import _pick_biggest_mover


def test_excludes_personal_symbol_even_when_biggest():
    # TSLA (watchlist-only) has the largest move but is NOT a registry asset
    # → must not headline the shared cockpit; the biggest GENERAL mover wins.
    rankings = [
        {"symbol": "TSLA", "ret_7d": -17.1},
        {"symbol": "SILVER", "ret_7d": -12.9},
        {"symbol": "BTC", "ret_7d": -2.8},
    ]
    general = {"BTC", "SILVER", "GOLD", "SPX", "DXY"}
    out = _pick_biggest_mover(rankings, general)
    assert out is not None
    assert out["symbol"] == "SILVER"
    assert out["ret_7d"] == -12.9


def test_general_symbol_can_win():
    rankings = [
        {"symbol": "SILVER", "ret_7d": -18.3},
        {"symbol": "TSLA", "ret_7d": -1.0},
    ]
    out = _pick_biggest_mover(rankings, {"SILVER", "BTC"})
    assert out["symbol"] == "SILVER"


def test_none_when_no_general_asset_present():
    out = _pick_biggest_mover([{"symbol": "TSLA", "ret_7d": -17.1}], {"BTC", "SILVER"})
    assert out is None


def test_empty_and_missing_fields_safe():
    assert _pick_biggest_mover([], {"BTC"}) is None
    assert _pick_biggest_mover(None, {"BTC"}) is None
    # row missing ret_7d shouldn't crash (treated as 0)
    out = _pick_biggest_mover([{"symbol": "BTC"}], {"BTC"})
    assert out["symbol"] == "BTC"


def test_case_insensitive_symbol_match():
    out = _pick_biggest_mover([{"symbol": "btc", "ret_7d": -5.0}], {"BTC"})
    assert out["symbol"] == "btc"
