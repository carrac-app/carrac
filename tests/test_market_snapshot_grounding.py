"""The public brief's LLM-authored Market Snapshot tables must
not restate deterministic numbers wrongly.

Covers:
(a) format_compact_asset_lines() now feeds the model the SAME 90-day
    range_pctile the rest of the report page renders deterministically
    (Cross-Asset Heat strip), instead of a different 20-bar high/low
    position — plus the scan_range_pctile_mismatch() grounding scan that
    catches any residual transcription error.
(b) scan_rotation_quadrant_mismatch() — catches a Rotation Scorecard row
    generalizing one asset's RRG quadrant onto a group of named assets.
"""
import logging

from carrac.grounding.hallucination_scan import (
    scan_range_pctile_mismatch,
    scan_rotation_quadrant_mismatch,
)


# --- format_compact_asset_lines() source fix ---------------------------------

def _ohlcv_with_percentile(n_bars=120, latest=110.0):
    """n_bars ascending closes 1.0..n_bars, then a final bar at `latest`.
    price_percentile(closes, 90) of the appended bar against the trailing 90
    is deterministic: all 90 preceding closes are < 110, so pct == 100.
    A 20-bar high/low RANGE POSITION over the same data is very different
    (last 20 bars run from n_bars-19..n_bars, so a 110 close sits well below
    that 20-bar high) — this is exactly the two-different-metrics defect.
    """
    bars = []
    for i in range(1, n_bars + 1):
        c = float(i)
        bars.append({"time": i, "open": c, "high": c, "low": c, "close": c})
    bars.append({"time": n_bars + 1, "open": latest, "high": latest, "low": latest, "close": latest})
    return bars


def test_compact_asset_lines_range_matches_deterministic_percentile():
    from morning_engine.data_assembly import format_compact_asset_lines
    from morning_engine.ta_facts import price_percentile

    ohlcv = _ohlcv_with_percentile()
    chart_raw = {("BTC", "1D"): {"ohlcv": ohlcv, "indicators": {}, "rsi": [], "cpr": {}}}
    text, pct_map = format_compact_asset_lines(chart_raw, ["BTC"])

    closes = [c["close"] for c in ohlcv]
    expected = price_percentile(closes, 90)
    assert expected is not None
    assert pct_map["BTC"] == expected
    assert f"Range {expected}%" in text

    # A 20-bar high/low position formula (wrong here) would print a
    # much smaller number here — assert the line is NOT using that formula.
    r20 = ohlcv[-20:]
    h20 = max(c["high"] for c in r20)
    l20 = min(c["low"] for c in r20)
    old_wrong_pos = round((ohlcv[-1]["close"] - l20) / (h20 - l20) * 100)
    assert old_wrong_pos != expected, "fixture must actually exercise the two-metrics defect"
    assert f"Range {old_wrong_pos}%" not in text


def test_compact_asset_lines_omits_range_without_enough_daily_history():
    from morning_engine.data_assembly import format_compact_asset_lines

    ohlcv = [{"time": i, "open": 1, "high": 1, "low": 1, "close": 1.0} for i in range(5)]
    chart_raw = {("ETH", "1D"): {"ohlcv": ohlcv, "indicators": {}, "rsi": [], "cpr": {}}}
    text, pct_map = format_compact_asset_lines(chart_raw, ["ETH"])
    assert "ETH" not in pct_map
    assert "Range n/a" in text


def test_compact_asset_lines_weekly_fallback_never_guesses_a_percentile():
    """A 1W-only fallback must not silently claim a 90-DAY percentile off 90
    WEEKLY bars — that would be a different, unlabeled window."""
    from morning_engine.data_assembly import format_compact_asset_lines

    ohlcv = _ohlcv_with_percentile()
    chart_raw = {("XRP", "1W"): {"ohlcv": ohlcv, "indicators": {}, "rsi": [], "cpr": {}}}
    text, pct_map = format_compact_asset_lines(chart_raw, ["XRP"])
    assert "XRP" not in pct_map
    assert "Range n/a" in text


# --- scan_range_pctile_mismatch() unit tests ----------------------------------

_SNAPSHOT_TABLE = (
    "## Market Snapshot\n"
    "| Asset | Price | Trend | RSI | Range | Key Signal |\n"
    "|---|---|---|---|---|---|\n"
    "| SPX | 6,450.10 | BULLISH | 61 | 98% | Above TC |\n"
    "| GOLD | 4,127.00 | NEUTRAL | 55 | 52% | Inside CPR |\n"
    "| BTC | 118,300.00 | BULLISH | 58 | 51% | golden cross |\n"
)

_GROUNDED = {"SPX": 98, "GOLD": 52, "BTC": 51}


def test_range_scan_clean_table_is_silent():
    assert scan_range_pctile_mismatch(_SNAPSHOT_TABLE, _GROUNDED) == []


def test_range_scan_flags_mismatched_cell():
    """An LLM-written SPX 83% must be flagged against the deterministic 98%."""
    bad = _SNAPSHOT_TABLE.replace("| SPX | 6,450.10 | BULLISH | 61 | 98% |",
                                   "| SPX | 6,450.10 | BULLISH | 61 | 83% |")
    out = scan_range_pctile_mismatch(bad, _GROUNDED)
    assert out and out[0]["type"] == "range_pctile_mismatch"
    assert "SPX" in out[0]["detail"]


def test_range_scan_flags_every_mismatched_row():
    bad = (_SNAPSHOT_TABLE
           .replace("| 98% |", "| 83% |")
           .replace("| 52% |", "| 89% |")
           .replace("| 51% |", "| 42% |"))
    out = scan_range_pctile_mismatch(bad, _GROUNDED)
    assets_flagged = {f["detail"].split(":")[0] for f in out}
    assert assets_flagged == {"SPX", "GOLD", "BTC"}


def test_range_scan_tolerates_small_rounding_drift():
    close_enough = _SNAPSHOT_TABLE.replace("| 98% |", "| 96% |")  # within default tol=5
    assert scan_range_pctile_mismatch(close_enough, _GROUNDED) == []


def test_range_scan_ignores_assets_without_ground_truth():
    """A spotlight asset with no entry in grounded_pct must never be flagged."""
    txt = "| Asset | Price | Trend | RSI | Range | Key Signal |\n|---|---|---|---|---|---|\n| QQQ | 1 | X | 1 | 5% |  |\n"
    assert scan_range_pctile_mismatch(txt, _GROUNDED) == []


def test_range_scan_no_grounding_no_crash():
    assert scan_range_pctile_mismatch(_SNAPSHOT_TABLE, {}) == []
    assert scan_range_pctile_mismatch("", _GROUNDED) == []


def test_range_scan_ignores_non_range_tables():
    """Table 2/3 headers (no 'Range' column) must never be scanned."""
    rs_table = (
        "| Rank | Asset | RS Score | 7-bar Return | Key Signal |\n"
        "|---|---|---|---|---|\n"
        "| 1 | SPX | 97.2 | 3.97% | strong |\n"
    )
    assert scan_range_pctile_mismatch(rs_table, {"SPX": 10}) == []


# --- scan_rotation_quadrant_mismatch() unit tests -----------------------------

_ROTATION_TABLE = (
    "## Market Snapshot\n"
    "| Sector | Status | Key Signal | Thesis Alignment |\n"
    "|---|---|---|---|\n"
    "| BTC / ETH | Leading | strong RS | confirms thesis |\n"
    "| TOTAL | Lagging | weak breadth | contradicts |\n"
)

_ROTATION_GROUNDED = {"BTC": "leading", "ETH": "leading", "TOTAL": "lagging"}


def test_rotation_scan_clean_table_is_silent():
    assert scan_rotation_quadrant_mismatch(_ROTATION_TABLE, _ROTATION_GROUNDED) == []


def test_rotation_scan_flags_generalized_quadrant():
    """A BTC/ETH/TOTAL row called 'Lagging'
    when BTC/ETH's grounded quadrant was actually 'leading' (TOTAL's
    quadrant was generalized onto the whole group)."""
    bad = (
        "| Sector | Status | Key Signal | Thesis Alignment |\n"
        "|---|---|---|---|\n"
        "| BTC/ETH/TOTAL | Lagging | weak breadth | contradicts |\n"
    )
    out = scan_rotation_quadrant_mismatch(bad, _ROTATION_GROUNDED)
    flagged = {f["detail"].split(":")[0] for f in out}
    assert flagged == {"BTC", "ETH"}
    assert all(f["type"] == "rotation_quadrant_mismatch" for f in out)


def test_rotation_scan_ignores_unnamed_symbols():
    txt = "| Sector | Status | Key Signal | Thesis Alignment |\n|---|---|---|---|\n| Macro & FX | Improving | mixed | neutral |\n"
    assert scan_rotation_quadrant_mismatch(txt, _ROTATION_GROUNDED) == []


def test_rotation_scan_no_grounding_no_crash():
    assert scan_rotation_quadrant_mismatch(_ROTATION_TABLE, {}) == []
    assert scan_rotation_quadrant_mismatch("", _ROTATION_GROUNDED) == []


def test_rotation_scan_ignores_range_table():
    """Table 1 (no 'Status' column) must never be scanned by the rotation check."""
    assert scan_rotation_quadrant_mismatch(_SNAPSHOT_TABLE, _ROTATION_GROUNDED) == []


# --- engine.py tripwire integration -------------------------------------------

def test_range_pctile_tripwire_logs_on_mismatch(caplog):
    from morning_engine.engine import _range_pctile_tripwire
    bad = _SNAPSHOT_TABLE.replace("| 98% |", "| 83% |")
    with caplog.at_level(logging.ERROR):
        _range_pctile_tripwire(bad, _GROUNDED, "general report")
    assert "range_pctile mismatch" in caplog.text


def test_range_pctile_tripwire_silent_when_clean(caplog):
    from morning_engine.engine import _range_pctile_tripwire
    with caplog.at_level(logging.ERROR):
        _range_pctile_tripwire(_SNAPSHOT_TABLE, _GROUNDED, "general report")
    assert "range_pctile mismatch" not in caplog.text


def test_range_pctile_tripwire_never_raises_on_bad_input(caplog):
    from morning_engine.engine import _range_pctile_tripwire
    with caplog.at_level(logging.ERROR):
        _range_pctile_tripwire(None, None, "general report")  # must not raise


def test_rotation_quadrant_tripwire_logs_on_mismatch(caplog, monkeypatch, notifications):
    import morning_engine.engine as engine_mod

    def _fake_api(path, **kwargs):
        assert path == "/api/relative-strength"
        return {"assets": [
            {"symbol": "BTC", "quadrant": "leading"},
            {"symbol": "ETH", "quadrant": "leading"},
            {"symbol": "TOTAL", "quadrant": "lagging"},
        ]}

    monkeypatch.setattr("morning_engine.data_assembly._api", _fake_api)
    bad = (
        "| Sector | Status | Key Signal | Thesis Alignment |\n"
        "|---|---|---|---|\n"
        "| BTC/ETH/TOTAL | Lagging | weak breadth | contradicts |\n"
    )
    with caplog.at_level(logging.ERROR):
        engine_mod._rotation_quadrant_tripwire(bad, "general report")
    assert "rotation quadrant mismatch" in caplog.text
    # Must reach the operator, not just the logfile.
    assert notifications


def test_rotation_quadrant_tripwire_silent_when_clean(caplog, monkeypatch):
    import morning_engine.engine as engine_mod

    def _fake_api(path, **kwargs):
        return {"assets": [
            {"symbol": "BTC", "quadrant": "leading"},
            {"symbol": "ETH", "quadrant": "leading"},
        ]}

    monkeypatch.setattr("morning_engine.data_assembly._api", _fake_api)
    with caplog.at_level(logging.ERROR):
        engine_mod._rotation_quadrant_tripwire(_ROTATION_TABLE, "general report")
    assert "rotation quadrant mismatch" not in caplog.text


def test_rotation_quadrant_tripwire_never_raises_when_api_fails(caplog, monkeypatch):
    import morning_engine.engine as engine_mod

    def _fake_api(path, **kwargs):
        return {"error": "timeout"}

    monkeypatch.setattr("morning_engine.data_assembly._api", _fake_api)
    with caplog.at_level(logging.ERROR):
        engine_mod._rotation_quadrant_tripwire(_ROTATION_TABLE, "general report")  # must not raise
