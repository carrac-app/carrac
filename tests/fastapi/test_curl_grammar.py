"""Grammar: /<symbol>[@window]?opts — parse, snap, validate."""
import pytest

from carrac.curl_charts.grammar import (
    BUCKETS,
    GrammarError,
    parse_options,
    parse_path,
    snap_window,
)


class TestParsePath:
    def test_bare_symbol(self):
        assert parse_path("/btc") == ("btc", None)

    def test_symbol_with_window(self):
        assert parse_path("/btc@10d") == ("btc", "10d")

    def test_uppercase_and_dots(self):
        assert parse_path("/BRK.B@30d") == ("brk.b", "30d")

    def test_rejects_multi_segment(self):
        with pytest.raises(GrammarError):
            parse_path("/btc/extra")

    def test_rejects_garbage_window(self):
        with pytest.raises(GrammarError):
            parse_path("/btc@blorp")

    def test_rejects_pure_punctuation_symbol(self):
        with pytest.raises(GrammarError):
            parse_path("/.")
        with pytest.raises(GrammarError):
            parse_path("/-")

    def test_rejects_root_path(self):
        with pytest.raises(GrammarError):
            parse_path("/")


class TestSnapWindow:
    def test_none_defaults_1d(self):
        assert snap_window(None).label == "1d"

    def test_exact_bucket(self):
        assert snap_window("10d").label == "10d"

    def test_snaps_4w_to_30d(self):
        assert snap_window("4w").label == "30d"

    def test_tie_rounds_up(self):
        # 2d = 48h is equidistant between 1d(24h) and 3d(72h) → up
        assert snap_window("2d").label == "3d"

    def test_buckets_define_tf_and_bars(self):
        b = snap_window("1d")
        assert b.timeframe == "1h"
        assert b.bars == 24

    def test_all_buckets_fit_default_width(self):
        # renderer capacity at 80 cols is (80-10)//2 == 35 candles
        for b in BUCKETS:
            assert b.bars <= 35, f"{b.label} requests {b.bars} bars"

    def test_bucket_bars_times_tf_equals_window(self):
        tf_hours = {"5m": 1 / 12, "15m": 0.25, "30m": 0.5, "1h": 1, "4h": 4, "1D": 24, "1W": 168, "1M": 720}
        # Approximate buckets: calendar weeks/months don't divide their windows exactly;
        # 1D×N would exceed the 35-bar capacity, so the nearest whole-candle count is used.
        approximate = {"90d", "1y"}
        for b in BUCKETS:
            if b.label in approximate:
                continue
            assert abs(b.bars * tf_hours[b.timeframe] - b.hours) < 1e-9, b.label

    def test_zero_window_rejected(self):
        with pytest.raises(GrammarError):
            snap_window("0d")


class TestParseOptions:
    def test_empty(self):
        o = parse_options("")
        assert (o.ema, o.plain, o.quiet, o.cols) == (False, False, False, 80)

    def test_concatenated_flags(self):
        o = parse_options("qT")
        assert o.quiet and o.plain

    def test_standard_form(self):
        o = parse_options("q&T&ema&cols=120")
        assert o.quiet and o.plain and o.ema and o.cols == 120

    def test_cols_snaps_to_allowed(self):
        assert parse_options("cols=110").cols == 120
        assert parse_options("cols=999").cols == 160

    def test_unknown_token_raises(self):
        with pytest.raises(GrammarError):
            parse_options("frobnicate")

    def test_cols_rejects_whitespace_and_sign(self):
        with pytest.raises(GrammarError):
            parse_options("cols=+80")
