"""Tests for carrac.facts.labels: asset name/level label lookups and threshold constants."""


from carrac.facts.labels import (
    asset_name,
    level_label,
    STRETCHED_PCT,
    DEPRESSED_PCT,
)


class TestAssetName:
    """Tests for asset_name() lookup helper."""

    def test_known_crypto_tickers(self):
        """asset_name returns friendly names for known crypto tickers."""
        assert asset_name("BTC") == "Bitcoin"
        assert asset_name("ETH") == "Ethereum"
        assert asset_name("SOL") == "Solana"
        assert asset_name("TOTAL") == "Total Crypto Cap"

    def test_known_equity_tickers(self):
        """asset_name returns friendly names for known equity tickers."""
        assert asset_name("SPX") == "S&P 500"
        assert asset_name("QQQ") == "Nasdaq 100"
        assert asset_name("MAGS") == "Magnificent 7"

    def test_known_commodity_tickers(self):
        """asset_name returns friendly names for known commodity tickers."""
        assert asset_name("OIL") == "Crude oil"
        assert asset_name("GOLD") == "Gold"
        assert asset_name("SILVER") == "Silver"
        assert asset_name("COPPER") == "Copper"

    def test_known_ratio_tickers(self):
        """asset_name returns friendly names for ratio tickers."""
        assert asset_name("GOLD_QQQ") == "Gold / Nasdaq"
        assert asset_name("QQQ_SPX") == "Nasdaq / S&P"

    def test_unknown_ticker_fallback(self):
        """asset_name falls back to the ticker itself for unknown symbols."""
        assert asset_name("FOO") == "FOO"
        assert asset_name("UNKNOWN") == "UNKNOWN"
        assert asset_name("XYZ") == "XYZ"

    def test_empty_string_ticker(self):
        """asset_name returns empty string for empty input."""
        assert asset_name("") == ""


class TestLevelLabel:
    """Tests for level_label() lookup helper."""

    def test_known_level_codes(self):
        """level_label returns human labels for known codes."""
        assert level_label("pdh") == "prior-day high"
        assert level_label("pdl") == "prior-day low"
        assert level_label("pwh") == "prior-week high"
        assert level_label("sma20") == "20-day average"
        assert level_label("sma200") == "200-day average"

    def test_level_label_case_insensitive(self):
        """level_label is case-insensitive."""
        assert level_label("PDH") == "prior-day high"
        assert level_label("Pdh") == "prior-day high"
        assert level_label("SMA200") == "200-day average"
        assert level_label("Sma50") == "50-day average"

    def test_cpr_and_resistance_levels(self):
        """level_label handles CPR and resistance/support levels."""
        assert level_label("pivot") == "monthly pivot"
        assert level_label("tc") == "monthly CPR top"
        assert level_label("bc") == "monthly CPR bottom"
        assert level_label("r1") == "monthly R1"
        assert level_label("s2") == "monthly S2"

    def test_unknown_code_fallback(self):
        """level_label falls back to upper-cased code for unknown codes."""
        assert level_label("xyz") == "XYZ"
        assert level_label("foo") == "FOO"
        assert level_label("unknown") == "UNKNOWN"

    def test_empty_string_code(self):
        """level_label returns empty string for empty input."""
        assert level_label("") == ""

    def test_unknown_code_all_caps_fallback(self):
        """level_label returns upper-cased code preserving unknown case input."""
        assert level_label("abc") == "ABC"
        assert level_label("EXISTING") == "EXISTING"


class TestThresholdConstants:
    """Tests for threshold constants."""

    def test_range_percentile_thresholds(self):
        """Range/percentile thresholds have expected values and ordering."""
        assert STRETCHED_PCT == 90
        assert DEPRESSED_PCT == 10
        assert STRETCHED_PCT > DEPRESSED_PCT
