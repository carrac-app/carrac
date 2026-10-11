"""Tests for the deterministic sentence library (carrac/facts/sentences.py).

The library renders the per-asset facts bundle into editorial sentences that
cannot hallucinate — every slot is a computed field. These tests pin the
skeleton: two reference builders + the assembler (salience sort, top-K,
graceful absence, deterministic variant selection, posture lint).
"""

from carrac.facts.sentences import (
    Sentence,
    build_ma_stack,
    build_rsi,
    build_streak,
    build_decisive_close,
    build_funding,
    build_stretch,
    build_candle,
    build_range,
    asset_sentences,
    asset_lede,
    _passes_posture,
)


# ---------------------------------------------------------------- ma_stack builder

def test_ma_stack_full_bull_renders_subject_and_duration():
    bundle = {
        "symbol": "BTC",
        "as_of": "2026-06-27",
        "ma_stack": {"state": "bullish", "bars_held": 6, "cross": "none"},
    }
    s = build_ma_stack(bundle, seed=0)
    assert s is not None
    assert isinstance(s, Sentence)
    assert "Bitcoin" in s.text   # friendly name, not raw ticker
    assert "6" in s.text         # the bars_held duration is surfaced
    assert s.salience > 0
    assert s.source              # a provenance tag is attached


def test_ma_stack_full_bear_renders():
    bundle = {
        "symbol": "OIL",
        "ma_stack": {"state": "bearish", "bars_held": 4, "cross": "none"},
    }
    s = build_ma_stack(bundle, seed=0)
    assert s is not None
    assert "Crude oil" in s.text  # friendly name, not raw ticker


def test_ma_stack_golden_cross_outranks_a_plain_stack():
    crossed = build_ma_stack(
        {"symbol": "ETH", "ma_stack": {"state": "bullish", "bars_held": 1, "cross": "golden_cross"}},
        seed=0,
    )
    plain = build_ma_stack(
        {"symbol": "ETH", "ma_stack": {"state": "bullish", "bars_held": 20, "cross": "none"}},
        seed=0,
    )
    assert crossed is not None and plain is not None
    assert "cross" in crossed.text.lower()
    assert crossed.salience > plain.salience


def test_ma_stack_insufficient_returns_none():
    bundle = {"symbol": "BTC", "ma_stack": {"state": "insufficient", "bars_held": 0, "cross": "none"}}
    assert build_ma_stack(bundle, seed=0) is None


def test_ma_stack_missing_block_returns_none():
    # Graceful absence: no ma_stack key at all -> no sentence (never an error/"N/A").
    assert build_ma_stack({"symbol": "BTC"}, seed=0) is None


# ---------------------------------------------------------------- rsi builder

def _divergence(dtype=None):
    return {
        "bullish_divergence": dtype == "bullish",
        "bearish_divergence": dtype == "bearish",
        "hidden_bullish_divergence": dtype == "hidden_bullish",
        "hidden_bearish_divergence": dtype == "hidden_bearish",
        "divergence_type": dtype,
    }


def test_rsi_overbought_with_divergence_renders_value_and_zone():
    bundle = {
        "symbol": "ETH",
        "as_of": "2026-06-27",
        "rsi": {"value": 78.0, "zone": "overbought", "divergence": _divergence("bearish")},
    }
    s = build_rsi(bundle, seed=0)
    assert s is not None
    assert "78" in s.text
    assert "overbought" in s.text.lower()
    assert s.salience > 0


def test_rsi_neutral_without_divergence_returns_none():
    # A neutral RSI with no divergence is not worth a sentence (gate on salience, not presence).
    bundle = {"symbol": "ETH", "rsi": {"value": 52.0, "zone": "neutral", "divergence": _divergence(None)}}
    assert build_rsi(bundle, seed=0) is None


def test_rsi_neutral_with_divergence_still_renders():
    bundle = {"symbol": "ETH", "rsi": {"value": 55.0, "zone": "neutral", "divergence": _divergence("bullish")}}
    s = build_rsi(bundle, seed=0)
    assert s is not None
    assert "divergence" in s.text.lower()


def test_rsi_missing_value_returns_none():
    assert build_rsi({"symbol": "ETH", "rsi": {"value": None, "zone": None, "divergence": _divergence(None)}}, seed=0) is None


# ---------------------------------------------------------------- assembler

def _rich_bundle():
    return {
        "symbol": "BTC",
        "as_of": "2026-06-27",
        "ma_stack": {"state": "bullish", "bars_held": 3, "cross": "none"},
        "rsi": {"value": 78.0, "zone": "overbought", "divergence": _divergence("bearish")},
    }


def test_assembler_drops_none_and_sorts_by_salience_desc():
    out = asset_sentences(_rich_bundle())
    assert len(out) >= 2
    assert all(isinstance(s, Sentence) for s in out)
    sal = [s.salience for s in out]
    assert sal == sorted(sal, reverse=True)


def test_assembler_top_k_limits():
    out = asset_sentences(_rich_bundle(), k=1)
    assert len(out) == 1


def test_assembler_quiet_bundle_returns_empty():
    # Nothing notable -> no sentences, no errors, no filler.
    quiet = {
        "symbol": "BTC",
        "ma_stack": {"state": "insufficient", "bars_held": 0, "cross": "none"},
        "rsi": {"value": 52.0, "zone": "neutral", "divergence": _divergence(None)},
    }
    assert asset_sentences(quiet) == []


def test_assembler_is_deterministic_for_same_symbol_and_asof():
    a = [s.text for s in asset_sentences(_rich_bundle())]
    b = [s.text for s in asset_sentences(_rich_bundle())]
    assert a == b


# ---------------------------------------------------------------- posture lint

def test_posture_lint_passes_descriptive_text():
    assert _passes_posture("BTC trades in a full bullish stack, intact for 6 sessions.") is True


def test_posture_lint_rejects_advice_phrasing():
    assert _passes_posture("You should buy BTC now.") is False
    assert _passes_posture("I recommend selling into this.") is False


# ---------------------------------------------------------------- build_streak

def test_streak_defending_renders_name_bars_and_level():
    bundle = {
        "symbol": "BTC",
        "streaks": [{"level": "sma200", "price": 101240.0, "bars": 4, "type": "defending"}],
    }
    s = build_streak(bundle, seed=0)
    assert s is not None
    assert "Bitcoin" in s.text
    assert "4" in s.text
    assert "200-day" in s.text   # level_label("sma200")
    assert s.salience > 0
    assert s.source


def test_streak_picks_entry_with_most_bars():
    bundle = {
        "symbol": "ETH",
        "streaks": [
            {"level": "sma50", "price": 3000.0, "bars": 2, "type": "testing"},
            {"level": "sma200", "price": 2500.0, "bars": 7, "type": "defending"},
        ],
    }
    s = build_streak(bundle, seed=0)
    assert s is not None
    assert "7" in s.text   # max bars = 7, not the 2-bar entry


def test_streak_salience_scales_with_bars():
    b2 = build_streak(
        {"symbol": "BTC", "streaks": [{"level": "sma200", "price": 100000.0, "bars": 2, "type": "defending"}]},
        seed=0,
    )
    b8 = build_streak(
        {"symbol": "BTC", "streaks": [{"level": "sma200", "price": 100000.0, "bars": 8, "type": "defending"}]},
        seed=0,
    )
    assert b2 is not None and b8 is not None
    assert b8.salience > b2.salience


def test_streak_type_map_all_produce_sentences():
    """Every streak type maps to a verb and produces a non-None sentence."""
    for t in ("defending", "testing", "rejecting", "capped"):
        bundle = {"symbol": "BTC", "streaks": [{"level": "pmh", "price": 100000.0, "bars": 3, "type": t}]}
        assert build_streak(bundle, seed=0) is not None


def test_streak_missing_returns_none():
    assert build_streak({"symbol": "BTC"}, seed=0) is None


def test_streak_empty_list_returns_none():
    assert build_streak({"symbol": "BTC", "streaks": []}, seed=0) is None


# ---------------------------------------------------------------- build_decisive_close

def test_decisive_close_renders_name_and_level_label():
    bundle = {
        "symbol": "GOLD",
        "decisive_close": [
            {"level": "pmh", "direction": "above", "confirmed": True, "bars_ago": 0},
        ],
    }
    s = build_decisive_close(bundle, seed=0)
    assert s is not None
    assert "Gold" in s.text
    assert "prior-month high" in s.text   # level_label("pmh")
    assert s.salience > 0
    assert s.source


def test_decisive_close_picks_smallest_bars_ago():
    bundle = {
        "symbol": "BTC",
        "decisive_close": [
            {"level": "pmh", "direction": "above", "confirmed": True, "bars_ago": 5},
            {"level": "sma200", "direction": "below", "confirmed": True, "bars_ago": 1},
        ],
    }
    s = build_decisive_close(bundle, seed=0)
    assert s is not None
    assert "200-day" in s.text   # bars_ago=1 is fresher than bars_ago=5


def test_decisive_close_salience_higher_for_fresher():
    s_fresh = build_decisive_close(
        {"symbol": "BTC", "decisive_close": [{"level": "pmh", "direction": "above", "confirmed": True, "bars_ago": 0}]},
        seed=0,
    )
    s_stale = build_decisive_close(
        {"symbol": "BTC", "decisive_close": [{"level": "pmh", "direction": "above", "confirmed": True, "bars_ago": 5}]},
        seed=0,
    )
    assert s_fresh is not None and s_stale is not None
    assert s_fresh.salience > s_stale.salience


def test_decisive_close_no_atr_cited():
    bundle = {
        "symbol": "BTC",
        "decisive_close": [{"level": "pmh", "direction": "above", "confirmed": True, "bars_ago": 0}],
    }
    s = build_decisive_close(bundle, seed=0)
    assert s is not None
    assert "ATR" not in s.text
    assert "atr" not in s.text.lower()


def test_decisive_close_missing_returns_none():
    assert build_decisive_close({"symbol": "BTC"}, seed=0) is None


def test_decisive_close_empty_list_returns_none():
    assert build_decisive_close({"symbol": "BTC", "decisive_close": []}, seed=0) is None


# ---------------------------------------------------------------- build_funding

def test_funding_renders_name_streak_and_percentile():
    bundle = {
        "symbol": "ETH",
        "funding": {
            "streak_days": 11, "sign": "positive", "latest": 0.01,
            "percentile": 88, "window_days": 90,
        },
    }
    s = build_funding(bundle, seed=0)
    assert s is not None
    assert "Ethereum" in s.text
    assert "11" in s.text
    assert "88" in s.text
    assert s.salience > 0
    assert s.source


def test_funding_salience_higher_at_extreme_percentile():
    s_extreme = build_funding(
        {"symbol": "ETH", "funding": {"streak_days": 10, "sign": "positive", "latest": 0.05,
                                       "percentile": 95, "window_days": 90}},
        seed=0,
    )
    s_mid = build_funding(
        {"symbol": "ETH", "funding": {"streak_days": 3, "sign": "positive", "latest": 0.01,
                                       "percentile": 55, "window_days": 90}},
        seed=0,
    )
    assert s_extreme is not None and s_mid is not None
    assert s_extreme.salience > s_mid.salience


def test_funding_missing_returns_none():
    assert build_funding({"symbol": "ETH"}, seed=0) is None


def test_funding_empty_dict_returns_none():
    assert build_funding({"symbol": "ETH", "funding": {}}, seed=0) is None


# ---------------------------------------------------------------- build_stretch

def test_stretch_renders_mayer_and_percentile():
    bundle = {
        "symbol": "BTC",
        "stretch": {"mayer": 1.12, "mayer_pct": 64},
    }
    s = build_stretch(bundle, seed=0)
    assert s is not None
    assert "Bitcoin" in s.text
    assert "1.12" in s.text
    assert "64" in s.text
    assert s.salience > 0
    assert s.source


def test_stretch_empty_dict_returns_none():
    assert build_stretch({"symbol": "BTC", "stretch": {}}, seed=0) is None


def test_stretch_missing_returns_none():
    assert build_stretch({"symbol": "BTC"}, seed=0) is None


def test_stretch_salience_higher_for_extreme_percentile():
    s_ext = build_stretch(
        {"symbol": "BTC", "stretch": {"mayer": 2.0, "mayer_pct": 95}},
        seed=0,
    )
    s_mid = build_stretch(
        {"symbol": "BTC", "stretch": {"mayer": 1.05, "mayer_pct": 55}},
        seed=0,
    )
    assert s_ext is not None and s_mid is not None
    assert s_ext.salience > s_mid.salience


# ---------------------------------------------------------------- build_candle

def test_candle_hammer_renders_name_and_label():
    bundle = {
        "symbol": "BTC",
        "candle": {
            "body_pct": 0.3, "upper_wick_ratio": 0.1, "lower_wick_ratio": 0.6,
            "close_in_range": 0.8, "gap_pct": 0.0, "pattern_label": "hammer",
        },
    }
    s = build_candle(bundle, seed=0)
    assert s is not None
    assert "Bitcoin" in s.text
    assert "hammer" in s.text.lower()
    assert s.salience > 0
    assert s.source


def test_candle_none_pattern_returns_none():
    bundle = {
        "symbol": "BTC",
        "candle": {
            "body_pct": 0.5, "upper_wick_ratio": 0.2, "lower_wick_ratio": 0.3,
            "close_in_range": 0.5, "gap_pct": 0.0, "pattern_label": None,
        },
    }
    assert build_candle(bundle, seed=0) is None


def test_candle_missing_returns_none():
    assert build_candle({"symbol": "BTC"}, seed=0) is None


def test_candle_doji_lower_salience_than_hammer():
    hammer = build_candle(
        {"symbol": "BTC", "candle": {"body_pct": 0.3, "upper_wick_ratio": 0.1,
                                      "lower_wick_ratio": 0.6, "close_in_range": 0.8,
                                      "gap_pct": 0.0, "pattern_label": "hammer"}},
        seed=0,
    )
    doji = build_candle(
        {"symbol": "BTC", "candle": {"body_pct": 0.05, "upper_wick_ratio": 0.5,
                                      "lower_wick_ratio": 0.45, "close_in_range": 0.5,
                                      "gap_pct": 0.0, "pattern_label": "doji"}},
        seed=0,
    )
    assert hammer is not None and doji is not None
    assert hammer.salience > doji.salience


def test_candle_all_patterns_are_posture_safe():
    for label in ("marubozu", "hammer", "inverted_hammer", "doji"):
        bundle = {
            "symbol": "BTC",
            "candle": {"body_pct": 0.5, "upper_wick_ratio": 0.2, "lower_wick_ratio": 0.3,
                       "close_in_range": 0.5, "gap_pct": 0.0, "pattern_label": label},
        }
        s = build_candle(bundle, seed=0)
        assert s is not None
        assert _passes_posture(s.text), f"posture failed for pattern '{label}': {s.text}"


# ---------------------------------------------------------------- build_range

def test_range_renders_pct_and_window():
    bundle = {
        "symbol": "BTC",
        "range_pctile": {"pct": 84, "window_days": 90},
    }
    s = build_range(bundle, seed=0)
    assert s is not None
    assert "Bitcoin" in s.text
    assert "84" in s.text
    assert "90" in s.text
    assert s.salience > 0
    assert s.source


def test_range_salience_higher_at_extremes():
    s_high = build_range(
        {"symbol": "BTC", "range_pctile": {"pct": 92, "window_days": 90}},
        seed=0,
    )
    s_mid = build_range(
        {"symbol": "BTC", "range_pctile": {"pct": 52, "window_days": 90}},
        seed=0,
    )
    assert s_high is not None and s_mid is not None
    assert s_high.salience > s_mid.salience


def test_range_missing_returns_none():
    assert build_range({"symbol": "BTC"}, seed=0) is None


def test_range_empty_dict_returns_none():
    assert build_range({"symbol": "BTC", "range_pctile": {}}, seed=0) is None


# ---------------------------------------------------------------- asset_lede

def test_asset_lede_returns_top_salience_text():
    bundle = {
        "symbol": "BTC",
        "as_of": "2026-06-27",
        "ma_stack": {"state": "bullish", "bars_held": 3, "cross": "none"},
        "rsi": {"value": 78.0, "zone": "overbought", "divergence": _divergence("bearish")},
    }
    text = asset_lede(bundle)
    assert text is not None
    assert isinstance(text, str)
    assert len(text) > 0
    # Must equal the text of the top sentence from asset_sentences
    top = asset_sentences(bundle, k=1)[0].text
    assert text == top


def test_asset_lede_quiet_bundle_returns_none():
    quiet = {
        "symbol": "BTC",
        "ma_stack": {"state": "insufficient", "bars_held": 0, "cross": "none"},
        "rsi": {"value": 52.0, "zone": "neutral", "divergence": _divergence(None)},
    }
    assert asset_lede(quiet) is None
