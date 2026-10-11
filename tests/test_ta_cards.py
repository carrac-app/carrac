"""Tests for morning_engine/ta_cards.py .

Covers:
  - render_asset_card with a full stub bundle
  - Graceful omission of missing fields (no crash, no 'N/A')
  - build_ta_cards_block: caps at 3, skips None bundles, returns "" when all None
  - Non-advice assertion: none of the banned advice words appear in the card
  - Ordinal suffix for the mayer percentile
  - State coverage: insufficient, bullish_partial, bearish_partial
"""
from __future__ import annotations


from morning_engine.ta_cards import render_asset_card, build_ta_cards_block, _ordinal, _fmt_price

# ---------------------------------------------------------------------------
# Banned advice words (non-advice contract)
# ---------------------------------------------------------------------------
_BANNED_WORDS = [
    "buy", "sell", "should", "enter", "exit", "target", "take profit",
]


def _assert_no_advice(text: str) -> None:
    lower = text.lower()
    for word in _BANNED_WORDS:
        assert word not in lower, (
            f"Advice word '{word}' found in card output:\n{text}"
        )


# ---------------------------------------------------------------------------
# Full stub bundle fixture
# ---------------------------------------------------------------------------

FULL_BUNDLE = {
    "symbol": "BTCUSDT",
    "as_of": "2026-06-26T07:30:00Z",
    "price": 105000.0,
    "ma_stack": {
        "state": "bullish",
        "bars_held": 42,
        "cross": "golden_cross",
    },
    "candle": {
        "body_pct": 0.65,
        "upper_wick_ratio": 0.1,
        "lower_wick_ratio": 0.05,
        "close_in_range": 0.8,
        "gap_pct": 0.0,
        "pattern_label": "bullish engulfing",
    },
    "volume": {
        "rvol": 1.8,
        "signal": "high",
        "reliable": True,
    },
    "rsi": {
        "value": 62.5,
        "zone": "neutral",
        "divergence": "none",
    },
    "stretch": {
        "mayer": 1.45,
        "mayer_pct": 72,
    },
    "levels": {
        "clusters": [
            {
                "price": 108000.0,
                "side": "resistance",
                "members": ["sma200", "pdh"],
                "count": 2,
                "dist_pct": 2.9,
            },
            {
                "price": 112000.0,
                "side": "resistance",
                "members": ["ath"],
                "count": 1,
                "dist_pct": 6.7,
            },
            {
                "price": 101500.0,
                "side": "support",
                "members": ["ema21", "vwap"],
                "count": 2,
                "dist_pct": -3.3,
            },
        ],
        "nearest_support_dist_pct": -3.3,
        "nearest_resistance_dist_pct": 2.9,
        "monthly_cpr": {"position": "above_bc"},
        "key_levels": {},
    },
    "streaks": [
        {
            "level": "sma200",
            "price": 98000.0,
            "bars": 15,
            "type": "defending",
        },
        {
            "level": "pdh",
            "price": 107500.0,
            "bars": 3,
            "type": "testing",
        },
    ],
    "decisive_close": [
        {
            "level": "sma50",
            "direction": "above",
            "confirmed": True,
            "bars_ago": 5,
        },
    ],
    "timeframes": ["1D", "1W"],
    "weekly": {
        "ma_stack": {
            "state": "bullish",
            "bars_held": 8,
            "cross": "none",
        },
        "candle": {},
        "streaks": [
            {
                "level": "sma50_weekly",
                "price": 95000.0,
                "bars": 6,
                "type": "defending",
            }
        ],
        "decisive_close": [],
    },
}

# ---------------------------------------------------------------------------
# Minimal stub bundle (no stretch, no clusters, no weekly)
# ---------------------------------------------------------------------------

MINIMAL_BUNDLE = {
    "symbol": "SOLUSDT",
    "as_of": "2026-06-26T07:30:00Z",
    "price": 185.5,
    "ma_stack": {
        "state": "bearish",
        "bars_held": 5,
        "cross": "none",
    },
    "candle": {
        "body_pct": 0.3,
        "pattern_label": None,
    },
    "volume": {
        "rvol": None,
        "signal": None,
        "reliable": False,
    },
    "rsi": {
        "value": None,
        "zone": None,
        "divergence": "none",
    },
    "stretch": {},
    "levels": {
        "clusters": [],
        "monthly_cpr": {},
    },
    "streaks": [],
    "decisive_close": [],
    "timeframes": ["1D"],
}


# ---------------------------------------------------------------------------
# Tests: render_asset_card — full bundle
# ---------------------------------------------------------------------------

class TestRenderAssetCardFull:
    def setup_method(self):
        self.card = render_asset_card(FULL_BUNDLE)

    def test_contains_symbol(self):
        assert "BTCUSDT" in self.card

    def test_contains_price(self):
        # 105,000 formatted
        assert "105,000" in self.card

    def test_contains_as_of_time(self):
        assert "07:30" in self.card

    def test_contains_ma_stack_bullish_label(self):
        assert "up stack" in self.card

    def test_contains_bars_held(self):
        assert "42 sessions" in self.card

    def test_contains_golden_cross(self):
        assert "golden cross" in self.card

    def test_contains_rsi_value(self):
        assert "RSI 63" in self.card or "RSI 62" in self.card  # rounded

    def test_contains_rsi_zone(self):
        assert "neutral" in self.card

    def test_contains_volume_rvol(self):
        assert "1.8×" in self.card

    def test_contains_volume_signal(self):
        assert "high" in self.card

    def test_contains_candle_pattern(self):
        assert "bullish engulfing" in self.card

    def test_contains_mayer_multiple(self):
        assert "1.45×" in self.card

    def test_contains_mayer_pct(self):
        assert "72nd percentile" in self.card

    def test_contains_mayer_band_mid_range(self):
        assert "mid-range" in self.card

    def test_contains_resistance_cluster_price(self):
        assert "108,000" in self.card

    def test_contains_resistance_cluster_members(self):
        assert "sma200+pdh" in self.card

    def test_contains_support_cluster_price(self):
        assert "101,500" in self.card

    def test_contains_cluster_dist_pct_positive(self):
        assert "+2.9%" in self.card

    def test_contains_cluster_dist_pct_negative(self):
        assert "-3.3%" in self.card

    def test_contains_price_divider(self):
        assert "── price" in self.card

    def test_contains_streak_defending(self):
        assert "defending" in self.card

    def test_contains_streak_bars(self):
        assert "for 15 sessions" in self.card

    def test_contains_streak_price(self):
        assert "98,000" in self.card

    def test_contains_decisive_close(self):
        assert "closed above" in self.card

    def test_contains_decisive_close_bars_ago(self):
        assert "5 sessions ago" in self.card

    def test_contains_monthly_cpr(self):
        assert "above bc" in self.card  # underscore replaced with space

    def test_contains_weekly_line(self):
        assert "Weekly:" in self.card

    def test_contains_weekly_ma_stack(self):
        assert "up stack" in self.card  # appears in weekly too

    def test_no_advice_words(self):
        _assert_no_advice(self.card)

    def test_no_na_placeholder(self):
        assert "N/A" not in self.card


# ---------------------------------------------------------------------------
# Tests: render_asset_card — minimal bundle (graceful omission)
# ---------------------------------------------------------------------------

class TestRenderAssetCardMinimal:
    def setup_method(self):
        self.card = render_asset_card(MINIMAL_BUNDLE)

    def test_renders_without_crash(self):
        assert isinstance(self.card, str)
        assert len(self.card) > 0

    def test_contains_symbol(self):
        assert "SOLUSDT" in self.card

    def test_no_stretch_section(self):
        assert "200-week" not in self.card

    def test_no_cluster_section(self):
        assert "resistance" not in self.card
        assert "support" not in self.card

    def test_no_weekly_section(self):
        assert "Weekly:" not in self.card

    def test_no_streak_section(self):
        assert "Level memory:" not in self.card

    def test_no_na_placeholder(self):
        assert "N/A" not in self.card

    def test_no_advice_words(self):
        _assert_no_advice(self.card)


# ---------------------------------------------------------------------------
# Tests: Ordinal suffix helper
# ---------------------------------------------------------------------------

class TestOrdinal:
    def test_ordinal_72_returns_nd(self):
        assert _ordinal(72) == "nd"

    def test_ordinal_21_returns_st(self):
        assert _ordinal(21) == "st"

    def test_ordinal_13_returns_th(self):
        assert _ordinal(13) == "th"

    def test_ordinal_64_returns_th(self):
        assert _ordinal(64) == "th"

    def test_ordinal_11_returns_th(self):
        assert _ordinal(11) == "th"

    def test_ordinal_12_returns_th(self):
        assert _ordinal(12) == "th"

    def test_ordinal_1_returns_st(self):
        assert _ordinal(1) == "st"

    def test_ordinal_2_returns_nd(self):
        assert _ordinal(2) == "nd"

    def test_ordinal_3_returns_rd(self):
        assert _ordinal(3) == "rd"


# ---------------------------------------------------------------------------
# Tests: State coverage (insufficient, bullish_partial, bearish_partial)
# ---------------------------------------------------------------------------

class TestRenderAssetCardInsufficientState:
    def setup_method(self):
        insufficient_bundle = {
            "symbol": "BTCUSDT",
            "as_of": "2026-06-26T07:30:00Z",
            "price": 50000.0,
            "ma_stack": {
                "state": "insufficient",
                "bars_held": None,
            },
            "candle": {},
            "volume": {},
            "rsi": {},
            "stretch": {},
            "levels": {"clusters": []},
            "streaks": [],
            "decisive_close": [],
            "timeframes": ["1D"],
        }
        self.card = render_asset_card(insufficient_bundle)

    def test_contains_insufficient_text(self):
        assert "insufficient history for the 200-day" in self.card

    def test_no_bars_held_for_insufficient(self):
        assert "sessions" not in self.card


class TestRenderAssetCardBullishPartial:
    def setup_method(self):
        partial_bundle = {
            "symbol": "ETHUSD",
            "as_of": "2026-06-26T07:30:00Z",
            "price": 3500.0,
            "ma_stack": {
                "state": "bullish_partial",
                "bars_held": 10,
            },
            "candle": {},
            "volume": {},
            "rsi": {},
            "stretch": {},
            "levels": {"clusters": []},
            "streaks": [],
            "decisive_close": [],
            "timeframes": ["1D"],
        }
        self.card = render_asset_card(partial_bundle)

    def test_contains_leaning_up(self):
        assert "leaning up" in self.card

    def test_contains_bars_held(self):
        assert "10 sessions" in self.card


class TestRenderAssetCardBearishPartial:
    def setup_method(self):
        partial_bundle = {
            "symbol": "SOLUSDT",
            "as_of": "2026-06-26T07:30:00Z",
            "price": 200.0,
            "ma_stack": {
                "state": "bearish_partial",
                "bars_held": 5,
            },
            "candle": {},
            "volume": {},
            "rsi": {},
            "stretch": {},
            "levels": {"clusters": []},
            "streaks": [],
            "decisive_close": [],
            "timeframes": ["1D"],
        }
        self.card = render_asset_card(partial_bundle)

    def test_contains_leaning_down(self):
        assert "leaning down" in self.card

    def test_contains_bars_held(self):
        assert "5 sessions" in self.card


class TestRenderAssetCardOneSidedClusters:
    def test_no_divider_when_only_resistances(self):
        """Price divider should not appear when clusters are one-sided (only resistances)."""
        bundle = {
            "symbol": "BTCUSDT",
            "as_of": "2026-06-26T07:30:00Z",
            "price": 50000.0,
            "ma_stack": {"state": "bullish"},
            "candle": {},
            "volume": {},
            "rsi": {},
            "stretch": {},
            "levels": {
                "clusters": [
                    {
                        "price": 52000.0,
                        "side": "resistance",
                        "members": ["sma200"],
                        "count": 1,
                        "dist_pct": 4.0,
                    },
                ],
            },
            "streaks": [],
            "decisive_close": [],
            "timeframes": ["1D"],
        }
        card = render_asset_card(bundle)
        assert "── price" not in card
        assert "resistance" in card

    def test_no_divider_when_only_supports(self):
        """Price divider should not appear when clusters are one-sided (only supports)."""
        bundle = {
            "symbol": "BTCUSDT",
            "as_of": "2026-06-26T07:30:00Z",
            "price": 50000.0,
            "ma_stack": {"state": "bullish"},
            "candle": {},
            "volume": {},
            "rsi": {},
            "stretch": {},
            "levels": {
                "clusters": [
                    {
                        "price": 48000.0,
                        "side": "support",
                        "members": ["ema21"],
                        "count": 1,
                        "dist_pct": -4.0,
                    },
                ],
            },
            "streaks": [],
            "decisive_close": [],
            "timeframes": ["1D"],
        }
        card = render_asset_card(bundle)
        assert "── price" not in card
        assert "support" in card

    def test_divider_when_both_sides(self):
        """Price divider should appear when there are both resistances and supports."""
        bundle = {
            "symbol": "BTCUSDT",
            "as_of": "2026-06-26T07:30:00Z",
            "price": 50000.0,
            "ma_stack": {"state": "bullish"},
            "candle": {},
            "volume": {},
            "rsi": {},
            "stretch": {},
            "levels": {
                "clusters": [
                    {
                        "price": 52000.0,
                        "side": "resistance",
                        "members": ["sma200"],
                        "count": 1,
                        "dist_pct": 4.0,
                    },
                    {
                        "price": 48000.0,
                        "side": "support",
                        "members": ["ema21"],
                        "count": 1,
                        "dist_pct": -4.0,
                    },
                ],
            },
            "streaks": [],
            "decisive_close": [],
            "timeframes": ["1D"],
        }
        card = render_asset_card(bundle)
        assert "── price" in card
        assert "resistance" in card
        assert "support" in card


# ---------------------------------------------------------------------------
# Tests: build_ta_cards_block
# ---------------------------------------------------------------------------

def _make_stub_getter(bundles_by_symbol: dict):
    """Return a getter function that returns a bundle dict or None per symbol."""
    def _getter(sym):
        return bundles_by_symbol.get(sym)
    return _getter


# ---------------------------------------------------------------------------
# Fix 1 (end-to-end) — dict-shaped cluster members must not crash render_asset_card
# ---------------------------------------------------------------------------

class TestRenderAssetCardWithDictClusterMembers:
    """End-to-end: zones with dict members → cluster_distances coerces →
    bundle has string members → render_asset_card does NOT raise TypeError."""

    def test_dict_members_does_not_raise(self):
        """cluster_distances coerces dict members; render_asset_card must not raise."""
        from morning_engine.ta_facts import cluster_distances
        zones = {
            "resistances": [{
                "price": 110.0,
                "members": [
                    {"code": "R1", "name": "Resistance 1",
                     "price": 110.0, "weight": 2.0, "family": "pivot"},
                    {"code": "SMA200", "name": "SMA 200",
                     "price": 110.0, "weight": 3.0, "family": "ma"},
                ],
                "count": 2,
            }],
            "supports": [{
                "price": 90.0,
                "members": [
                    {"code": "POC", "name": "Point of Control",
                     "price": 90.0, "weight": 3.0, "family": "vp"},
                ],
                "count": 1,
            }],
        }
        cluster_result = cluster_distances(zones, 100.0)
        bundle = {
            "symbol": "XAUUSD",
            "price": 100.0,
            "as_of": "2026-06-26T07:30:00Z",
            "ma_stack": {"state": "bullish", "bars_held": 5, "cross": "none"},
            "candle": {},
            "volume": {},
            "rsi": {},
            "stretch": {},
            "levels": {
                "clusters": cluster_result["clusters"],
            },
            "streaks": [],
            "decisive_close": [],
        }
        card = render_asset_card(bundle)   # must NOT raise TypeError
        # Codes are joined in the card output
        assert "R1+SMA200" in card
        assert "POC" in card


# ---------------------------------------------------------------------------
# Fix 3 — candle pattern label: underscores replaced with spaces
# ---------------------------------------------------------------------------

class TestCandlePatternUnderscore:
    def test_inverted_hammer_rendered_with_spaces(self):
        """inverted_hammer → 'inverted hammer candle' (underscore removed)."""
        bundle = {
            **FULL_BUNDLE,
            "candle": {"pattern_label": "inverted_hammer"},
        }
        card = render_asset_card(bundle)
        assert "inverted hammer candle" in card
        assert "inverted_hammer" not in card

    def test_hammer_no_underscore_unchanged(self):
        """Patterns without underscores are unaffected."""
        bundle = {**FULL_BUNDLE, "candle": {"pattern_label": "hammer"}}
        card = render_asset_card(bundle)
        assert "hammer candle" in card

    def test_marubozu_unchanged(self):
        """marubozu has no underscore → unchanged."""
        bundle = {**FULL_BUNDLE, "candle": {"pattern_label": "marubozu"}}
        card = render_asset_card(bundle)
        assert "marubozu candle" in card


class TestBuildTaCardsBlock:
    def test_returns_empty_string_when_all_none(self):
        getter = _make_stub_getter({})
        result = build_ta_cards_block(["A", "B", "C"], get_facts=getter)
        assert result == ""

    def test_skips_none_bundles(self):
        getter = _make_stub_getter({"A": FULL_BUNDLE, "B": None, "C": None, "D": None})
        result = build_ta_cards_block(["A", "B", "C", "D"], get_facts=getter)
        assert "BTCUSDT" in result  # A's bundle

    def test_caps_at_3_cards(self):
        bundles = {s: {**FULL_BUNDLE, "symbol": s} for s in ["A", "B", "C", "D"]}
        getter = _make_stub_getter(bundles)
        result = build_ta_cards_block(["A", "B", "C", "D"], get_facts=getter)
        # There should be at most 3 "### " headers
        assert result.count("### ") <= 3

    def test_includes_cards_for_symbols_with_bundles(self):
        a_bundle = {**FULL_BUNDLE, "symbol": "A"}
        c_bundle = {**FULL_BUNDLE, "symbol": "C", "price": 50000.0}
        getter = _make_stub_getter({"A": a_bundle, "B": None, "C": c_bundle, "D": None})
        result = build_ta_cards_block(["A", "B", "C", "D"], get_facts=getter)
        assert "### A" in result
        assert "### C" in result
        assert "### B" not in result
        assert "### D" not in result

    def test_contains_heading(self):
        getter = _make_stub_getter({"A": FULL_BUNDLE})
        result = build_ta_cards_block(["A"], get_facts=getter)
        assert "## Spotlight" in result

    def test_no_advice_words_in_block(self):
        getter = _make_stub_getter({"A": FULL_BUNDLE})
        result = build_ta_cards_block(["A"], get_facts=getter)
        _assert_no_advice(result)

    def test_defaults_get_facts_is_callable(self):
        """Passing no get_facts should not crash (it will return None from Redis miss)."""
        # This just checks the default path is importable; Redis is not running in tests.
        # The block returns "" since Redis is not available.
        result = build_ta_cards_block(["BTCUSDT"])
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Fix 1 — _fmt_price: magnitude-aware precision
# ---------------------------------------------------------------------------

class TestFmtPrice:
    def test_large_integer_no_decimals(self):
        assert _fmt_price(61240) == "$61,240"

    def test_two_decimal_for_dollars(self):
        assert _fmt_price(3.5) == "$3.50"

    def test_four_decimal_for_sub_dollar(self):
        assert _fmt_price(0.203) == "$0.2030"

    def test_eight_decimal_for_sub_cent(self):
        assert _fmt_price(0.00001234) == "$0.00001234"

    def test_none_returns_em_dash(self):
        assert _fmt_price(None) == "$—"

    def test_thousand_boundary(self):
        assert _fmt_price(1000) == "$1,000"

    def test_just_below_one_uses_four_dp(self):
        assert _fmt_price(0.5) == "$0.5000"

    def test_zero_uses_eight_dp(self):
        assert _fmt_price(0) == "$0.00000000"


# ---------------------------------------------------------------------------
# Fix 2 — decisive-close curation
# ---------------------------------------------------------------------------

# 12 decisive closes: 4 recent (bars_ago <= 21) + 8 ancient.
_DECISIVE_12_BUNDLE = {
    "symbol": "BTCUSDT",
    "price": 50000.0,
    "as_of": "2026-06-26T07:30:00Z",
    "ma_stack": {"state": "bullish"},
    "candle": {},
    "volume": {},
    "rsi": {},
    "stretch": {},
    "levels": {"clusters": []},
    "streaks": [],
    "decisive_close": [
        {"level": "sma50",  "direction": "above", "bars_ago": 3},
        {"level": "sma200", "direction": "above", "bars_ago": 7},
        {"level": "ema21",  "direction": "below", "bars_ago": 15},
        {"level": "pdh",    "direction": "above", "bars_ago": 21},
        {"level": "r1",     "direction": "above", "bars_ago": 50},
        {"level": "r2",     "direction": "below", "bars_ago": 100},
        {"level": "s1",     "direction": "above", "bars_ago": 150},
        {"level": "s2",     "direction": "below", "bars_ago": 200},
        {"level": "ath",    "direction": "above", "bars_ago": 300},
        {"level": "atl",    "direction": "below", "bars_ago": 506},
        {"level": "vwap",   "direction": "above", "bars_ago": 700},
        {"level": "mfi",    "direction": "below", "bars_ago": 925},
    ],
    "timeframes": ["1D"],
}


class TestDecisiveCloseCuration:
    def setup_method(self):
        self.card = render_asset_card(_DECISIVE_12_BUNDLE)

    def test_at_most_3_decisive_shown(self):
        count = self.card.count("**Confirmed:**")
        assert count <= 3

    def test_exactly_3_decisive_shown(self):
        """4 qualify (bars_ago 3,7,15,21); only the first 3 are shown."""
        count = self.card.count("**Confirmed:**")
        assert count == 3

    def test_ancient_entries_excluded(self):
        for ago in ("50 sessions ago", "100 sessions ago", "506 sessions ago",
                    "925 sessions ago", "700 sessions ago"):
            assert ago not in self.card, f"Ancient entry '{ago}' must not appear"

    def test_ordered_most_recent_first(self):
        pos_3 = self.card.index("3 sessions ago")
        pos_7 = self.card.index("7 sessions ago")
        pos_15 = self.card.index("15 sessions ago")
        assert pos_3 < pos_7 < pos_15


class TestAllAncientDecisive:
    def test_no_confirmed_section_when_all_ancient(self):
        """When every decisive close is > 21 bars old, no Confirmed: line appears."""
        bundle = {
            "symbol": "BTCUSDT",
            "price": 50000.0,
            "as_of": "2026-06-26T07:30:00Z",
            "ma_stack": {"state": "bullish"},
            "candle": {},
            "volume": {},
            "rsi": {},
            "stretch": {},
            "levels": {"clusters": []},
            "streaks": [],
            "decisive_close": [
                {"level": "sma50",  "direction": "above", "bars_ago": 50},
                {"level": "sma200", "direction": "below", "bars_ago": 506},
                {"level": "ema21",  "direction": "above", "bars_ago": 925},
            ],
            "timeframes": ["1D"],
        }
        card = render_asset_card(bundle)
        assert "**Confirmed:**" not in card


# ---------------------------------------------------------------------------
# Fix 2 — streak curation
# ---------------------------------------------------------------------------

# 5 streaks with varying bars; longest 3 should be kept, shortest 2 dropped.
_STREAKS_5_BUNDLE = {
    "symbol": "ETHUSD",
    "price": 3500.0,
    "as_of": "2026-06-26T07:30:00Z",
    "ma_stack": {"state": "bullish"},
    "candle": {},
    "volume": {},
    "rsi": {},
    "stretch": {},
    "levels": {"clusters": []},
    "streaks": [
        {"level": "sma200", "price": 3000.0, "bars": 30, "type": "defending"},
        {"level": "ema21",  "price": 3400.0, "bars":  5, "type": "testing"},
        {"level": "vwap",   "price": 3100.0, "bars": 20, "type": "defending"},
        {"level": "pdh",    "price": 3450.0, "bars":  2, "type": "testing"},
        {"level": "sma50",  "price": 3200.0, "bars": 10, "type": "defending"},
    ],
    "decisive_close": [],
    "timeframes": ["1D"],
}


class TestStreakCuration:
    def setup_method(self):
        self.card = render_asset_card(_STREAKS_5_BUNDLE)

    def test_at_most_3_streaks_shown(self):
        count = self.card.count("**Level memory:**")
        assert count <= 3

    def test_exactly_3_streaks_shown(self):
        count = self.card.count("**Level memory:**")
        assert count == 3

    def test_streaks_ordered_longest_first(self):
        pos_30 = self.card.index("for 30 sessions")
        pos_20 = self.card.index("for 20 sessions")
        pos_10 = self.card.index("for 10 sessions")
        assert pos_30 < pos_20 < pos_10

    def test_shortest_streaks_excluded(self):
        assert "for 5 sessions" not in self.card
        assert "for 2 sessions" not in self.card


# ---------------------------------------------------------------------------
# Funding card line tests
# ---------------------------------------------------------------------------

_BUNDLE_WITH_FUNDING = {
    **FULL_BUNDLE,
    "funding": {
        "sign": "positive",
        "streak_days": 12,
        "latest": 0.05,
        "percentile": 88,
        "window_days": 89,
    },
}

_BUNDLE_WITHOUT_FUNDING = {
    **FULL_BUNDLE,
    # No "funding" key
}


class TestFundingCardLine:
    def test_funding_line_present_when_funding_key_exists(self):
        """Bundle with funding → **Funding:** line appears."""
        card = render_asset_card(_BUNDLE_WITH_FUNDING)
        assert "**Funding:**" in card

    def test_funding_line_contains_sign(self):
        """Funding line includes the sign string."""
        card = render_asset_card(_BUNDLE_WITH_FUNDING)
        assert "positive" in card.lower()

    def test_funding_line_contains_sessions(self):
        """Funding line includes streak_days as '12 sessions'."""
        card = render_asset_card(_BUNDLE_WITH_FUNDING)
        assert "12 sessions" in card

    def test_funding_line_contains_ordinal_percentile(self):
        """Funding line renders percentile with ordinal suffix (88 → 88th)."""
        card = render_asset_card(_BUNDLE_WITH_FUNDING)
        assert "88th" in card

    def test_funding_line_absent_when_no_funding_key(self):
        """Bundle without funding key → no **Funding:** line."""
        card = render_asset_card(_BUNDLE_WITHOUT_FUNDING)
        assert "**Funding:**" not in card

    def test_no_advice_words_with_funding(self):
        """Non-advice contract still holds when funding line is rendered."""
        card = render_asset_card(_BUNDLE_WITH_FUNDING)
        _assert_no_advice(card)

    def test_ordinal_pct_1st(self):
        """percentile=1 → '1st pct'."""
        bundle = {**FULL_BUNDLE, "funding": {"sign": "positive", "streak_days": 3, "latest": 0.01, "percentile": 1, "window_days": 30}}
        card = render_asset_card(bundle)
        assert "1st" in card

    def test_ordinal_pct_2nd(self):
        """percentile=2 → '2nd pct'."""
        bundle = {**FULL_BUNDLE, "funding": {"sign": "positive", "streak_days": 3, "latest": 0.01, "percentile": 2, "window_days": 30}}
        card = render_asset_card(bundle)
        assert "2nd" in card

    def test_ordinal_pct_11th(self):
        """percentile=11 → '11th pct' (special case)."""
        bundle = {**FULL_BUNDLE, "funding": {"sign": "negative", "streak_days": 5, "latest": -0.02, "percentile": 11, "window_days": 30}}
        card = render_asset_card(bundle)
        assert "11th" in card

    def test_negative_funding_sign_in_card(self):
        """Negative sign renders correctly."""
        bundle = {**FULL_BUNDLE, "funding": {"sign": "negative", "streak_days": 7, "latest": -0.03, "percentile": 15, "window_days": 89}}
        card = render_asset_card(bundle)
        assert "**Funding:**" in card
        assert "negative" in card.lower()
        assert "7 sessions" in card
        assert "15th" in card
