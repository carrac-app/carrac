"""Cluster spotlight picker (editorial v4): one slot per registry category."""
from carrac.reports.routes import _pick_cluster_spotlight

CATS = {"Crypto": ["BTC", "ETH"], "Equities": ["SPX"], "Commodities": ["GOLD"], "Intermarket": ["DXY"]}

def test_one_per_cluster_by_salience():
    ranked = {"BTC": 2.0, "ETH": 5.0, "SPX": 1.0, "GOLD": 3.0, "DXY": 0.5}
    bundles = {s: {"symbol": s} for s in ranked}
    picks = _pick_cluster_spotlight(CATS, bundles, ranked)
    assert [(c, s) for c, s, _ in picks] == [
        ("Intermarket", "DXY"), ("Equities", "SPX"), ("Commodities", "GOLD"), ("Crypto", "ETH")]

def test_empty_cluster_skipped():
    bundles = {"BTC": {"symbol": "BTC"}}
    picks = _pick_cluster_spotlight(CATS, bundles, {"BTC": 1.0})
    assert [(c, s) for c, s, _ in picks] == [("Crypto", "BTC")]
