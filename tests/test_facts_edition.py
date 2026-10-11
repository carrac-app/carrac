"""Tests for carrac.facts.edition — the comparative, cluster and spotlight ledes."""
from __future__ import annotations

from carrac.facts.edition import (
    comparative_lede,
    cluster_lede,
    spotlight_lede,
)
from carrac.facts.sentences import asset_lede


# ══════════════════════════════════════════════════════════════════════════════
# 1. comparative_lede
# ══════════════════════════════════════════════════════════════════════════════

class TestComparativeLede:
    """Comparative lede: why THIS asset, not another — one standout sentence."""

    def _base(self, symbol, **overrides):
        """Bundle with all dimensions at mid-range — not notable on any axis."""
        b = {
            "symbol": symbol,
            "ma_stack": {"state": "bullish", "bars_held": 3, "cross": "none"},
            "rsi": {"value": 55.0, "zone": "neutral", "divergence": {}},
            "stretch": {"mayer": 1.2, "mayer_pct": 50},
            "range_pctile": {"pct": 50},
            "funding": {"sign": "positive", "streak_days": 3, "percentile": 60},
        }
        b.update(overrides)
        return b

    # ── guard cases ───────────────────────────────────────────────────────────

    def test_fewer_than_two_assets_returns_none(self):
        bundles = {"BTC": self._base("BTC")}
        assert comparative_lede("BTC", bundles) is None

    def test_symbol_absent_returns_none(self):
        bundles = {
            "BTC": self._base("BTC"),
            "ETH": self._base("ETH"),
        }
        assert comparative_lede("SOL", bundles) is None

    def test_no_standout_returns_none(self):
        """Both assets identical and mid-range — neither is distinctive."""
        bundles = {
            "BTC": self._base("BTC"),
            "ETH": self._base("ETH"),
        }
        assert comparative_lede("BTC", bundles) is None

    # ── most_stretched_low (priority 2) ───────────────────────────────────────

    def test_most_stretched_low(self):
        bundles = {
            "ETH": self._base("ETH", stretch={"mayer": 0.65, "mayer_pct": 3}),
            "BTC": self._base("BTC"),  # mayer_pct=50
        }
        assert comparative_lede("ETH", bundles) == (
            "Ethereum is the most stretched name on the board — "
            "trading at 0.65× its 200-week average, the 3rd percentile of its history."
        )

    def test_most_stretched_low_boundary_exactly_10(self):
        bundles = {
            "ETH": self._base("ETH", stretch={"mayer": 0.80, "mayer_pct": 10}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("ETH", bundles) == (
            "Ethereum is the most stretched name on the board — "
            "trading at 0.80× its 200-week average, the 10th percentile of its history."
        )

    def test_most_stretched_low_threshold_not_met(self):
        """mayer_pct=11 is above the ≤10 threshold → None."""
        bundles = {
            "ETH": self._base("ETH", stretch={"mayer": 0.85, "mayer_pct": 11}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("ETH", bundles) is None

    def test_most_stretched_low_not_the_standout(self):
        """ETH has low mayer_pct but BTC is even lower → ETH is not the standout."""
        bundles = {
            "ETH": self._base("ETH", stretch={"mayer": 0.65, "mayer_pct": 8}),
            "BTC": self._base("BTC", stretch={"mayer": 0.55, "mayer_pct": 4}),
        }
        assert comparative_lede("ETH", bundles) is None

    # ── most_stretched_high (priority 3) ──────────────────────────────────────

    def test_most_stretched_high(self):
        bundles = {
            "SOL": self._base("SOL", stretch={"mayer": 2.50, "mayer_pct": 95}),
            "BTC": self._base("BTC"),  # mayer_pct=50
        }
        assert comparative_lede("SOL", bundles) == (
            "Solana is the most extended name on the board — "
            "2.50× its 200-week average (95th percentile)."
        )

    def test_most_stretched_high_boundary_exactly_90(self):
        bundles = {
            "SOL": self._base("SOL", stretch={"mayer": 2.0, "mayer_pct": 90}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("SOL", bundles) == (
            "Solana is the most extended name on the board — "
            "2.00× its 200-week average (90th percentile)."
        )

    def test_most_stretched_high_threshold_not_met(self):
        """mayer_pct=89 is below ≥90 threshold → None."""
        bundles = {
            "SOL": self._base("SOL", stretch={"mayer": 2.0, "mayer_pct": 89}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("SOL", bundles) is None

    # ── lowest_range (priority 4) ─────────────────────────────────────────────

    def test_lowest_range(self):
        bundles = {
            "XRP": self._base("XRP", range_pctile={"pct": 4}),
            "BTC": self._base("BTC"),  # range_pctile=50
        }
        assert comparative_lede("XRP", bundles) == (
            "XRP is lower in its 90-day range than any other tracked asset "
            "— the 4th percentile."
        )

    def test_lowest_range_boundary_exactly_10(self):
        bundles = {
            "XRP": self._base("XRP", range_pctile={"pct": 10}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("XRP", bundles) == (
            "XRP is lower in its 90-day range than any other tracked asset "
            "— the 10th percentile."
        )

    def test_lowest_range_threshold_not_met(self):
        """pct=11 is above the ≤10 threshold."""
        bundles = {
            "XRP": self._base("XRP", range_pctile={"pct": 11}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("XRP", bundles) is None

    # ── highest_range (priority 5) ────────────────────────────────────────────

    def test_highest_range(self):
        bundles = {
            "GOLD": self._base("GOLD", range_pctile={"pct": 93}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("GOLD", bundles) == (
            "Gold is higher in its 90-day range than any other tracked asset "
            "— the 93rd percentile."
        )

    def test_highest_range_boundary_exactly_90(self):
        bundles = {
            "GOLD": self._base("GOLD", range_pctile={"pct": 90}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("GOLD", bundles) == (
            "Gold is higher in its 90-day range than any other tracked asset "
            "— the 90th percentile."
        )

    def test_highest_range_threshold_not_met(self):
        """pct=89 is below ≥90 threshold."""
        bundles = {
            "GOLD": self._base("GOLD", range_pctile={"pct": 89}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("GOLD", bundles) is None

    # ── extreme_funding (priority 1) ──────────────────────────────────────────

    def test_extreme_funding(self):
        bundles = {
            "SPX": self._base(
                "SPX",
                funding={"sign": "negative", "streak_days": 5, "percentile": 5},
                range_pctile={"pct": 20},
            ),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("SPX", bundles) == (
            "S&P 500 is the only name pairing a depressed price with "
            "deeply negative funding — an unusually rare combination."
        )

    def test_extreme_funding_range_boundary_exactly_25(self):
        """range_pctile=25 is at the ≤25 boundary — qualifies."""
        bundles = {
            "SPX": self._base(
                "SPX",
                funding={"sign": "negative", "streak_days": 5, "percentile": 5},
                range_pctile={"pct": 25},
            ),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("SPX", bundles) == (
            "S&P 500 is the only name pairing a depressed price with "
            "deeply negative funding — an unusually rare combination."
        )

    def test_extreme_funding_range_too_high(self):
        """Negative funding but range_pctile=30 (>25) — price not depressed enough."""
        bundles = {
            "SPX": self._base(
                "SPX",
                funding={"sign": "negative", "streak_days": 5, "percentile": 5},
                range_pctile={"pct": 30},
            ),
            "BTC": self._base("BTC"),
        }
        # range_pctile=30 > 25; SPX is otherwise mid-range → None
        assert comparative_lede("SPX", bundles) is None

    def test_extreme_funding_not_standout(self):
        """BTC has more-negative funding (lower percentile) than SPX → SPX not standout."""
        bundles = {
            "SPX": self._base(
                "SPX",
                funding={"sign": "negative", "streak_days": 5, "percentile": 10},
                range_pctile={"pct": 20},
            ),
            "BTC": self._base(
                "BTC",
                funding={"sign": "negative", "streak_days": 3, "percentile": 4},
                range_pctile={"pct": 20},
            ),
        }
        assert comparative_lede("SPX", bundles) is None

    def test_extreme_funding_positive_sign_not_eligible(self):
        """Symbol has positive funding sign — not eligible for extreme_funding."""
        bundles = {
            "SPX": self._base(
                "SPX",
                funding={"sign": "positive", "streak_days": 5, "percentile": 5},
                range_pctile={"pct": 20},
            ),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("SPX", bundles) is None

    # ── longest_trend (priority 6) ────────────────────────────────────────────

    def test_longest_trend_bull(self):
        bundles = {
            "QQQ": self._base("QQQ", ma_stack={"state": "bullish", "bars_held": 15, "cross": "none"}),
            "BTC": self._base("BTC"),  # bars_held=3
        }
        assert comparative_lede("QQQ", bundles) == (
            "Nasdaq 100 has held its bull stack longer than any other name — 15 sessions."
        )

    def test_longest_trend_bear(self):
        bundles = {
            "TLT": self._base("TLT", ma_stack={"state": "bearish", "bars_held": 20, "cross": "none"}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("TLT", bundles) == (
            "20Y Treasuries has held its bear stack longer than any other name — 20 sessions."
        )

    def test_longest_trend_boundary_exactly_10(self):
        bundles = {
            "QQQ": self._base("QQQ", ma_stack={"state": "bullish", "bars_held": 10, "cross": "none"}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("QQQ", bundles) == (
            "Nasdaq 100 has held its bull stack longer than any other name — 10 sessions."
        )

    def test_longest_trend_threshold_not_met(self):
        """bars_held=9 is below the ≥10 threshold."""
        bundles = {
            "QQQ": self._base("QQQ", ma_stack={"state": "bullish", "bars_held": 9, "cross": "none"}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("QQQ", bundles) is None

    # ── most_oversold (priority 7) ────────────────────────────────────────────

    def test_most_oversold(self):
        bundles = {
            "TLT": self._base("TLT", rsi={"value": 22.0, "zone": "oversold", "divergence": {}}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("TLT", bundles) == (
            "20Y Treasuries is the most oversold name on the board — RSI 22."
        )

    def test_most_oversold_zone_neutral_not_eligible(self):
        """Low RSI value but zone is neutral — does not count as oversold standout."""
        bundles = {
            "TLT": self._base("TLT", rsi={"value": 22.0, "zone": "neutral", "divergence": {}}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("TLT", bundles) is None

    def test_most_oversold_not_standout(self):
        """BTC is more oversold than TLT — TLT is not the standout."""
        bundles = {
            "TLT": self._base("TLT", rsi={"value": 28.0, "zone": "oversold", "divergence": {}}),
            "BTC": self._base("BTC", rsi={"value": 20.0, "zone": "oversold", "divergence": {}}),
        }
        assert comparative_lede("TLT", bundles) is None

    # ── most_overbought (priority 8) ──────────────────────────────────────────

    def test_most_overbought(self):
        bundles = {
            "DXY": self._base("DXY", rsi={"value": 78.0, "zone": "overbought", "divergence": {}}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("DXY", bundles) == (
            "Dollar index is the most overbought name on the board — RSI 78."
        )

    def test_most_overbought_zone_neutral_not_eligible(self):
        """High RSI value but zone is neutral — not eligible."""
        bundles = {
            "DXY": self._base("DXY", rsi={"value": 78.0, "zone": "neutral", "divergence": {}}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("DXY", bundles) is None

    # ── priority ordering ─────────────────────────────────────────────────────

    def test_extreme_funding_beats_stretched_low(self):
        """extreme_funding (priority 1) wins over most_stretched_low (priority 2)."""
        bundles = {
            "ETH": self._base(
                "ETH",
                stretch={"mayer": 0.65, "mayer_pct": 3},       # qualifies as most_stretched_low
                funding={"sign": "negative", "streak_days": 5, "percentile": 5},
                range_pctile={"pct": 20},                        # also qualifies as extreme_funding
            ),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("ETH", bundles) == (
            "Ethereum is the only name pairing a depressed price with "
            "deeply negative funding — an unusually rare combination."
        )

    def test_stretched_low_beats_lowest_range(self):
        """most_stretched_low (priority 2) wins over lowest_range (priority 4)."""
        bundles = {
            "ETH": self._base(
                "ETH",
                stretch={"mayer": 0.65, "mayer_pct": 3},  # qualifies as most_stretched_low
                range_pctile={"pct": 5},                    # qualifies as lowest_range
            ),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("ETH", bundles) == (
            "Ethereum is the most stretched name on the board — "
            "trading at 0.65× its 200-week average, the 3rd percentile of its history."
        )

    def test_lowest_range_beats_longest_trend(self):
        """lowest_range (priority 4) wins over longest_trend (priority 6)."""
        bundles = {
            "XRP": self._base(
                "XRP",
                range_pctile={"pct": 4},                                          # qualifies
                ma_stack={"state": "bullish", "bars_held": 15, "cross": "none"},  # qualifies
            ),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("XRP", bundles) == (
            "XRP is lower in its 90-day range than any other tracked asset "
            "— the 4th percentile."
        )

    def test_longest_trend_beats_most_oversold(self):
        """longest_trend (priority 6) wins over most_oversold (priority 7)."""
        bundles = {
            "TLT": self._base(
                "TLT",
                ma_stack={"state": "bearish", "bars_held": 15, "cross": "none"},  # qualifies
                rsi={"value": 22.0, "zone": "oversold", "divergence": {}},         # qualifies
            ),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("TLT", bundles) == (
            "20Y Treasuries has held its bear stack longer than any other name — 15 sessions."
        )

    def test_most_oversold_beats_most_overbought_not_applicable(self):
        """An asset can only be oversold OR overbought — both cannot fire simultaneously."""
        # If zone=oversold, the overbought check is skipped (zone mismatch).
        bundles = {
            "TLT": self._base("TLT", rsi={"value": 22.0, "zone": "oversold", "divergence": {}}),
            "BTC": self._base("BTC"),
        }
        assert comparative_lede("TLT", bundles) == (
            "20Y Treasuries is the most oversold name on the board — RSI 22."
        )

    # ── robustness: missing fields ────────────────────────────────────────────

    def test_missing_stretch_skips_dimension(self):
        """No stretch key in bundle — dimension silently excluded, no error."""
        bundles = {
            "BTC": {
                "symbol": "BTC",
                "ma_stack": {"state": "bullish", "bars_held": 3},
                "rsi": {"value": 55.0, "zone": "neutral"},
                "funding": {"sign": "positive", "percentile": 60},
            },
            "ETH": {
                "symbol": "ETH",
                "ma_stack": {"state": "bullish", "bars_held": 3},
                "rsi": {"value": 55.0, "zone": "neutral"},
                "funding": {"sign": "positive", "percentile": 60},
            },
        }
        assert comparative_lede("BTC", bundles) is None

    def test_empty_stretch_dict_skips_dimension(self):
        """stretch={} skips that dimension cleanly."""
        bundles = {
            "BTC": self._base("BTC", stretch={}),
            "ETH": self._base("ETH", stretch={}),
        }
        assert comparative_lede("BTC", bundles) is None

    def test_missing_range_pctile_skips_dimension(self):
        """No range_pctile → skips range checks, no error."""
        bundles = {
            "BTC": self._base("BTC", range_pctile=None),
            "ETH": self._base("ETH", range_pctile=None),
        }
        assert comparative_lede("BTC", bundles) is None

    def test_three_assets_correct_standout(self):
        """With 3 assets, the correct standout is returned."""
        bundles = {
            "BTC": self._base("BTC"),
            "ETH": self._base("ETH", stretch={"mayer": 0.65, "mayer_pct": 3}),
            "SOL": self._base("SOL", stretch={"mayer": 1.1, "mayer_pct": 40}),
        }
        assert comparative_lede("ETH", bundles) == (
            "Ethereum is the most stretched name on the board — "
            "trading at 0.65× its 200-week average, the 3rd percentile of its history."
        )
        # BTC and SOL are NOT the standout on this dimension
        assert comparative_lede("BTC", bundles) is None
        assert comparative_lede("SOL", bundles) is None

    def test_tied_for_min_symbol_counts_as_standout(self):
        """When symbol is tied for the minimum mayer_pct, it still counts as standout."""
        bundles = {
            "ETH": self._base("ETH", stretch={"mayer": 0.65, "mayer_pct": 5}),
            "BTC": self._base("BTC", stretch={"mayer": 0.68, "mayer_pct": 5}),  # same min
        }
        # ETH is tied for min (5 == 5) — counts as standout
        result = comparative_lede("ETH", bundles)
        assert result == (
            "Ethereum is the most stretched name on the board — "
            "trading at 0.65× its 200-week average, the 5th percentile of its history."
        )


# ══════════════════════════════════════════════════════════════════════════════
# 2. cluster_lede — peer-group superlative when no GLOBAL superlative fired
# ══════════════════════════════════════════════════════════════════════════════

def _eb(symbol, rp=50, rsi=55.0, zone="neutral", mayer=None, mayer_pct=None,
        bars=5, state="bullish", funding=None):
    """Editorial test bundle with selective field presence."""
    b = {
        "symbol": symbol,
        "ma_stack": {"state": state, "bars_held": bars, "cross": "none"},
        "rsi": {"value": rsi, "zone": zone, "divergence": {}},
        "range_pctile": {"pct": rp},
    }
    if mayer is not None:
        b["stretch"] = {"mayer": mayer, "mayer_pct": mayer_pct}
    if funding is not None:
        b["funding"] = funding
    return b


def _commodities():
    """A commodity cluster mirroring the real risk-off day."""
    return {
        "GOLD": _eb("GOLD", rp=3, rsi=37.6, state="bearish", bars=3),
        "SILVER": _eb("SILVER", rp=2, rsi=32.4, state="bearish", bars=8),
        "OIL": _eb("OIL", rp=3, rsi=27.4, zone="oversold", state="bearish", bars=4),
        "COPPER": _eb("COPPER", rp=61, rsi=46.6, bars=8),
        "NATGAS": _eb("NATGAS", rp=93, rsi=55.8, state="bearish_partial", bars=46),
    }


class TestClusterLede:
    def test_weakest_commodity_by_range(self):
        # Silver (2nd) is strictly lower than every other commodity → cluster-weakest
        assert cluster_lede("SILVER", _commodities()) == (
            "Silver is the weakest of the commodity names — "
            "the 2nd percentile of its 90-day range."
        )

    def test_not_weakest_returns_none(self):
        # Gold (3rd) is not the cluster minimum — Silver (2nd) is lower
        assert cluster_lede("GOLD", _commodities()) is None

    def test_tied_for_weakest_counts(self):
        b = {
            "GOLD": _eb("GOLD", rp=2, state="bearish"),
            "SILVER": _eb("SILVER", rp=2, state="bearish"),
            "COPPER": _eb("COPPER", rp=61),
        }
        # Gold ties Silver for the minimum (2 == 2) → still the cluster-weakest
        assert cluster_lede("GOLD", b) == (
            "Gold is the weakest of the commodity names — "
            "the 2nd percentile of its 90-day range."
        )

    def test_strongest_commodity_by_range_high_side(self):
        # Natural gas (93rd) is the cluster max and >= 90
        assert cluster_lede("NATGAS", _commodities()) == (
            "Natural gas is the strongest of the commodity names — "
            "the 93rd percentile of its 90-day range."
        )

    def test_most_oversold_in_cluster(self):
        # Non-extreme range so the range angles skip; OIL is the lone oversold name
        b = {
            "OIL": _eb("OIL", rp=40, rsi=27.0, zone="oversold", state="bearish"),
            "GOLD": _eb("GOLD", rp=45, rsi=38.0),
            "COPPER": _eb("COPPER", rp=55, rsi=46.0),
        }
        assert cluster_lede("OIL", b) == (
            "Crude oil is the most oversold of the commodity names — RSI 27."
        )

    def test_longest_trend_in_cluster(self):
        # Non-extreme range; NATGAS holds the longest stack (>= 10)
        b = {
            "NATGAS": _eb("NATGAS", rp=45, state="bearish_partial", bars=46),
            "GOLD": _eb("GOLD", rp=50, bars=3),
            "COPPER": _eb("COPPER", rp=55, bars=8),
        }
        assert cluster_lede("NATGAS", b) == (
            "Natural gas has held its bear stack longest of the commodity names — "
            "46 sessions."
        )

    def test_single_present_member_returns_none(self):
        # Only one commodity present → no peer group
        b = {"GOLD": _eb("GOLD", rp=3), "BTC": _eb("BTC", rp=1)}
        assert cluster_lede("GOLD", b) is None

    def test_symbol_not_in_any_cluster_returns_none(self):
        assert cluster_lede("DXY", _commodities() | {"DXY": _eb("DXY", rp=97)}) is None

    def test_symbol_absent_returns_none(self):
        assert cluster_lede("PLATINUM", _commodities()) is None

    def test_crypto_cluster_weakest(self):
        b = {
            "BTC": _eb("BTC", rp=1),
            "ETH": _eb("ETH", rp=4),
            "XRP": _eb("XRP", rp=3),
            "TOTAL": _eb("TOTAL", rp=2),
        }
        # BTC is the crypto-cluster minimum
        assert cluster_lede("BTC", b) == (
            "Bitcoin is the weakest of the crypto names — "
            "the 1st percentile of its 90-day range."
        )


# ══════════════════════════════════════════════════════════════════════════════
# 3. spotlight_lede — global → cluster → honest-range / asset_lede chain
# ══════════════════════════════════════════════════════════════════════════════

class TestSpotlightLede:
    def _board(self):
        """Whole-board fixture resembling the real risk-off day."""
        b = {
            "BTC": _eb("BTC", rp=1, mayer=1.05, mayer_pct=15, funding={"sign": "positive", "percentile": 58, "latest": 0.01, "streak_days": 3}),
            "ETH": _eb("ETH", rp=4, mayer=0.64, mayer_pct=0, funding={"sign": "positive", "percentile": 19, "latest": 0.005, "streak_days": 2}),
            "XRP": _eb("XRP", rp=3, bars=12, funding={"sign": "negative", "percentile": 1, "latest": -0.0001, "streak_days": 7}),
        }
        b.update(_commodities())
        b["TOTAL"] = _eb("TOTAL", rp=2, rsi=33.7, state="bearish", bars=5)
        return b

    def test_global_superlative_wins(self):
        # ETH is the most-stretched globally → spotlight returns the GLOBAL lede
        board = self._board()
        assert spotlight_lede("ETH", board) == comparative_lede("ETH", board)
        assert "most stretched name on the board" in spotlight_lede("ETH", board)

    def test_cluster_fallback_when_no_global(self):
        # Silver wins no global superlative (BTC is globally lowest-range) but is
        # the cluster-weakest commodity → spotlight returns the CLUSTER lede
        board = self._board()
        assert comparative_lede("SILVER", board) is None
        assert spotlight_lede("SILVER", board) == cluster_lede("SILVER", board)
        assert spotlight_lede("SILVER", board) == (
            "Silver is the weakest of the commodity names — "
            "the 2nd percentile of its 90-day range."
        )

    def test_extreme_no_superlative_uses_honest_range_line(self):
        # Gold wins neither global nor cluster, but is range-extreme → honest
        # breadth-aware line (NOT a misleading 1-day bounce from asset_lede)
        board = self._board()
        assert comparative_lede("GOLD", board) is None
        assert cluster_lede("GOLD", board) is None
        assert spotlight_lede("GOLD", board) == (
            "Gold sits at the 3rd percentile of its 90-day range — "
            "pinned to its lows alongside much of the tape."
        )

    def test_total_extreme_gets_honest_line_not_bounce(self):
        board = self._board()
        out = spotlight_lede("TOTAL", board)
        # Must lead with the dominant range fact, never a bullish-sounding bounce
        assert "2nd percentile" in out
        assert "decisive close above" not in out

    def test_non_extreme_uses_asset_lede(self):
        # A mid-range asset (wins no superlative, not range-extreme) keeps its
        # salient single-asset sentence rather than a generic range line.
        # NATGAS holds the longest trend so SPX wins no GLOBAL superlative.
        spx = _eb("SPX", rp=64, rsi=46.1, state="bearish", bars=4)
        board = {
            "SPX": spx,
            "QQQ": _eb("QQQ", rp=68, bars=4),
            "NATGAS": _eb("NATGAS", rp=45, state="bearish_partial", bars=46),
        }
        al = asset_lede(spx)
        assert al  # fixture sanity: there IS a salient single-asset fact
        assert spotlight_lede("SPX", board) == al

    def test_never_returns_none_for_present_asset(self):
        board = self._board()
        for sym in board:
            out = spotlight_lede(sym, board)
            assert isinstance(out, str) and out
