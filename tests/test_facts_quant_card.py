"""Tests for carrac/facts/quant_card.py — quant signals for the editorial report.

Two pure shapers over a build_snapshot() result:
  quant_rows(snapshot, symbol)  — the focus asset's per-asset z-score extremes (card)
  market_anomalies(snapshot)    — the market-wide _GLOBAL_ z-score extremes (sidebar)

Lead-lag was deliberately dropped. No I/O — tested
against a hand-built snapshot fixture.
"""

from __future__ import annotations

from carrac.facts.quant_card import quant_rows, market_anomalies


def _snapshot():
    """build_snapshot()-shaped. anomalies[] is GLOBAL — mixes per-asset rows
    (asset == ticker) and market-wide rows (asset == '_GLOBAL_')."""
    return {
        "anomalies": [
            {"domain": "derivatives", "metric": "DVOL_BTC", "asset": "BTC",
             "zscore": 2.3, "percentile": 96.0, "value": 55.0, "date": "d"},
            {"domain": "onchain", "metric": "MVRV_ETH", "asset": "ETH",
             "zscore": 3.1, "percentile": 99.0, "value": 2.4, "date": "d"},
            {"domain": "derivatives", "metric": "SKEW_25D_BTC", "asset": "BTC",
             "zscore": -2.6, "percentile": 3.0, "value": -2.0, "date": "d"},
            {"domain": "derivatives", "metric": "BASIS_BTC", "asset": "BTC",
             "zscore": 2.05, "percentile": 92.0, "value": 0.1, "date": "d"},
            {"domain": "macro", "metric": "REVERSE_REPO", "asset": "_GLOBAL_",
             "zscore": 9.45, "percentile": 100.0, "value": 1.0, "date": "d"},
            {"domain": "macro", "metric": "YIELD_10Y2Y", "asset": "_GLOBAL_",
             "zscore": -3.37, "percentile": 1.0, "value": -0.1, "date": "d"},
            {"domain": "macro", "metric": "COT_DXY_COMM_NET", "asset": "_GLOBAL_",
             "zscore": -2.87, "percentile": 4.0, "value": -1.0, "date": "d"},
        ],
    }


# ─────────────────────────────── per-asset extremes (quant_rows) ───────────────

def test_extremes_only_for_focus_symbol():
    out = quant_rows(_snapshot(), "BTC")
    assert isinstance(out, list)
    metrics = [e["metric"] for e in out]
    assert "MVRV_ETH" not in metrics            # that's ETH's
    assert "REVERSE_REPO" not in metrics        # that's _GLOBAL_
    assert all(m.endswith("_BTC") for m in metrics)


def test_extremes_sorted_by_abs_z_and_capped():
    out = quant_rows(_snapshot(), "BTC")          # default cap 2
    assert len(out) == 2
    assert out[0]["metric"] == "SKEW_25D_BTC"     # |−2.6| strongest
    assert out[1]["metric"] == "DVOL_BTC"         # |2.3| next
    assert out[0]["zscore"] == -2.6
    assert out[0]["percentile"] == 3.0


def test_extremes_cap_configurable():
    assert len(quant_rows(_snapshot(), "BTC", max_extremes=3)) == 3


def test_extremes_empty_for_symbol_without_anomalies():
    assert quant_rows(_snapshot(), "SOL") == []


def test_extreme_metric_label_humanized():
    by_metric = {e["metric"]: e["label"] for e in quant_rows(_snapshot(), "BTC")}
    assert by_metric["SKEW_25D_BTC"] == "25Δ option skew"
    assert by_metric["DVOL_BTC"] == "Implied volatility"


def test_unknown_metric_acronym_aware_fallback():
    snap = {"anomalies": [
        {"domain": "x", "metric": "ETF_FLOW_BTC", "asset": "BTC",
         "zscore": 2.4, "percentile": 97.0, "value": 1.0, "date": "d"}]}
    # acronyms stay upper-cased (not "Etf flow"); asset suffix stripped
    assert quant_rows(snap, "BTC")[0]["label"] == "ETF Flow"


def test_quant_rows_missing_snapshot_is_empty():
    assert quant_rows(None, "BTC") == []
    assert quant_rows({}, "BTC") == []


# ─────────────────────────────── market anomalies (_GLOBAL_) ───────────────────

def test_market_anomalies_only_global():
    out = market_anomalies(_snapshot())
    assert isinstance(out, list)
    metrics = [a["metric"] for a in out]
    assert "DVOL_BTC" not in metrics        # per-asset, excluded
    assert set(metrics) == {"REVERSE_REPO", "YIELD_10Y2Y", "COT_DXY_COMM_NET"}


def test_market_anomalies_sorted_and_capped():
    out = market_anomalies(_snapshot(), max_rows=2)
    assert len(out) == 2
    assert out[0]["metric"] == "REVERSE_REPO"   # |9.45| strongest
    assert out[1]["metric"] == "YIELD_10Y2Y"    # |−3.37| next


def test_market_anomaly_labels_humanized():
    by_metric = {a["metric"]: a["label"] for a in market_anomalies(_snapshot())}
    assert by_metric["REVERSE_REPO"] == "Reverse repo (Fed)"
    assert by_metric["YIELD_10Y2Y"] == "10Y-2Y curve"
    # unknown global falls back to acronym-aware clean (COT/DXY preserved)
    assert by_metric["COT_DXY_COMM_NET"] == "COT DXY Comm Net"


def test_market_anomalies_missing_snapshot_is_empty():
    assert market_anomalies(None) == []
    assert market_anomalies({}) == []
