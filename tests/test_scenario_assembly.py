"""Tests for the min-N gate in scenario assembly.

These tests cover the pure-logic passes_min_n() helper.

NOTE: Importing morning_engine.data_assembly requires Flask context (via
morning_engine.config → carrac.assets.registry). These tests are designed to
run inside the deployed container where pytest + Flask are available.

To run locally (system Python, no pytest/Flask):
    python3 -c "
    def passes_min_n(result, min_n=25):
        return bool(result) and int(result.get('instances', 0)) >= min_n
    assert passes_min_n({'instances': 40}) is True
    assert passes_min_n({'instances': 12}) is False
    assert passes_min_n({'instances': 0}) is False
    assert passes_min_n({}) is False
    assert passes_min_n({'instances': 20}, min_n=15) is True
    print('passes_min_n: all assertions ok')
    "
"""

from morning_engine.data_assembly import passes_min_n


def test_min_n_gate_blocks_small_samples():
    assert passes_min_n({"instances": 40}) is True
    assert passes_min_n({"instances": 12}) is False
    assert passes_min_n({"instances": 0}) is False
    assert passes_min_n({}) is False


def test_min_n_gate_custom_threshold():
    assert passes_min_n({"instances": 20}, min_n=15) is True


def test_min_n_gate_exact_boundary():
    # Exactly at the threshold should pass (>=, not >)
    assert passes_min_n({"instances": 25}) is True
    assert passes_min_n({"instances": 24}) is False


def test_min_n_gate_none_result():
    assert passes_min_n(None) is False


def test_min_n_gate_with_string_instances():
    # Result dicts from the API may have string-encoded integers
    assert passes_min_n({"instances": "30"}) is True
    assert passes_min_n({"instances": "10"}) is False


def _asset_tf_data(cpr):
    bar = {"time": 1752710400, "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0}
    return {"ohlcv": [dict(bar), dict(bar, close=106.0)], "cpr": cpr}


def test_format_asset_tf_tolerates_none_cpr_levels():
    # A present-but-None S/R level must not crash the report,
    # and must not be printed as a fake 0.00 level either.
    from morning_engine.data_assembly import _format_asset_tf

    cpr = {"pivot": 104.0, "tc": 105.5, "bc": 102.5,
           "s1": None, "s2": 98.0, "s3": None, "r1": 108.0, "r2": None, "r3": None}
    text = _format_asset_tf(_asset_tf_data(cpr), "BTC", "1D")
    assert "Monthly CPR" in text
    assert "S2=98.00" in text
    assert "R1=108.00" in text
    assert "S1=" not in text
    assert "=0.00" not in text


def test_format_asset_tf_skips_cpr_block_when_tc_or_bc_none():
    from morning_engine.data_assembly import _format_asset_tf

    cpr = {"pivot": 104.0, "tc": None, "bc": 102.5, "s1": 99.0, "r1": 108.0}
    text = _format_asset_tf(_asset_tf_data(cpr), "BTC", "1D")
    assert "Monthly CPR" not in text


def test_format_asset_tf_skips_none_history_pivot():
    from morning_engine.data_assembly import _format_asset_tf

    cpr = {"pivot": 104.0, "tc": 105.5, "bc": 102.5, "s1": 99.0, "r1": 108.0,
           "history": [{"periodStart": 1749945600, "pivot": None},
                       {"periodStart": 1747267200, "pivot": 101_500.0}]}
    text = _format_asset_tf(_asset_tf_data(cpr), "BTC", "1D")
    assert "CPR History" in text
    assert "101,500" in text
