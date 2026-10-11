"""Never splice a live price onto dead history.

inject_synthetic_candle() exists so today's price shows on the chart before the
daily bar closes. It is guarded by a PRICE plausibility band (0.5x-1.5x of
the recent median), which catches a frozen ticker price wildly
detached from its history.

That band asks "is this price near the recent ones". It never asks whether the
recent ones are still ALIVE. A ticker whose collection stopped (MKR, flat at 1499.0 since 2026-06-09)
has a live price of 1305.92, which is 0.871x the median, so it passes cleanly
and would be spliced onto weeks-old flatline history.

What that produces on the MCP server:
    decisive_close: 8 entries, every one "confirmed": true
    key_levels:     pdh/pdl/mh/ml ALL 1499.0 (the only value in the history)
    rsi:            3.7, "oversold"
i.e. a confident, well-formatted breakdown call manufactured out of a dead
flatline plus one live price — on a public channel, with nothing in the
payload marking it stale.
"""
import pandas as pd
import pytest
from datetime import datetime


NOW = datetime(2026, 8, 5)


def _frozen_flatline():
    """MKR as it actually was: four identical bars, last one 2026-06-09.

    The volumes here are REAL on purpose. This fixture stands for
    history that stopped — bars that were genuine when they were written and
    have simply not been added to since. Giving them volume 0 (as this fixture
    originally did) quietly made them indistinguishable from the synthetic
    zero-volume rows in ``_persisted_synthetic_tail``, so a check that told the
    two apart would still have looked green here while doing nothing.
    The prices stay identical — that part really was the shape on disk.
    """
    return pd.DataFrame(
        {"open": [1499.0] * 4, "high": [1499.0] * 4, "low": [1499.0] * 4,
         "close": [1499.0] * 4, "volume": [812.0, 640.5, 733.25, 918.0]},
        index=pd.to_datetime(["2026-06-06", "2026-06-07", "2026-06-08", "2026-06-09"]),
    )


def _persisted_synthetic_tail(price, dates):
    """A RUN of injected candles that made it onto disk: OHLC identical, volume 0.

    This is the production shape found earlier and the fixtures above never
    modelled — AGIX carried 761 consecutive rows of it, MKR 9, and every one
    counted as a real bar to a check that only looked at the timestamp.
    """
    n = len(dates)
    return pd.DataFrame(
        {"open": [price] * n, "high": [price] * n, "low": [price] * n,
         "close": [price] * n, "volume": [0.0] * n},
        index=pd.to_datetime(dates),
    )


def _fresh_series(end=None):
    """Three consecutive daily bars ending on ``end`` — today by default.

    The default is relative to the wall clock ON PURPOSE. The
    ``TestSpliceIsRejectedOnStaleHistory`` cases below go through
    ``get_multi_timeframe_data_with_realtime``, which reads the real clock
    internally and cannot be handed a ``now=``. A hardcoded index therefore
    does not stay fresh: a fixture ending a day before it was written passes the
    5-day gate, then starts failing on the day it ages to 6.

    Pass ``end=NOW`` where the assertion pins its own reference instead.
    """
    end = pd.Timestamp(end).normalize() if end is not None else pd.Timestamp.today().normalize()
    return pd.DataFrame(
        {"open": [62000.0, 62500.0, 63000.0], "high": [62800.0, 63200.0, 63400.0],
         "low": [61500.0, 62100.0, 62600.0], "close": [62500.0, 63000.0, 62900.0],
         "volume": [1000.0, 1100.0, 900.0]},
        index=pd.date_range(end=end, periods=3, freq="D"),
    )


class TestHistoryStalenessGate:
    def test_stale_history_is_recognised_despite_a_plausible_price(self):
        """The exact MKR numbers. 0.871x sails through the price band, so the
        price check alone can never catch this class."""
        from carrac.precompute import _is_history_stale

        df = _frozen_flatline()
        ratio = 1305.92 / df["close"].tail(5).median()
        assert 0.5 < ratio < 1.5, "precondition: the old guard would have allowed it"
        assert _is_history_stale(df, 'AGIX', now=NOW) is True

    def test_fresh_history_is_not_flagged(self):
        """Guard against fixing the leak by breaking the feature — live prices
        must still reach the chart for assets that are actually collecting."""
        from carrac.precompute import _is_history_stale
        assert _is_history_stale(_fresh_series(end=NOW), 'BTC', now=NOW) is False

    def test_a_previously_injected_candle_cannot_mask_staleness(self):
        """TODAY's synthetic bar must not mask dead history.

        This case is handled by the date test alone — the bar is dated today,
        so "the last row strictly before today" already skips it. Kept as the
        pin for that half of the rule; see the sibling test below
        for the half it does NOT cover.
        """
        from carrac.precompute import _is_history_stale

        df = _frozen_flatline()
        today = pd.Timestamp(NOW.date())
        injected = pd.DataFrame(
            {"open": [1305.92], "high": [1305.92], "low": [1305.92],
             "close": [1305.92], "volume": [0.0]}, index=[today])
        df = pd.concat([df, injected]).sort_index()

        assert _is_history_stale(df, 'AGIX', now=NOW) is True

    def test_a_RUN_of_persisted_synthetic_bars_cannot_mask_staleness(self):
        """The case the date test cannot see, and production had.

        Once an injected candle is PERSISTED, tomorrow it is dated in the past,
        so "the last row strictly before today" happily returns it. Each new
        synthetic bar then keeps the series looking 1 day old forever, which
        re-authorises the next one. On prod this ran for 761 consecutive days
        on AGIX; MKR, EOS, COLLAT and FTM were mid-accumulation.
        """
        from carrac.precompute import _is_history_stale

        df = pd.concat([
            _frozen_flatline(),
            _persisted_synthetic_tail(1225.0, pd.date_range("2026-06-10", "2026-08-04")),
        ]).sort_index()

        # The trap: the newest row is only a day old, so a date-only check
        # calls this fresh even though nothing real has arrived since June.
        assert (pd.Timestamp(NOW.date()) - df.index.max()).days <= 1
        assert _is_history_stale(df, 'MKR', now=datetime(2026, 8, 5)) is True

    def test_a_derived_ratio_series_is_not_mistaken_for_fabrication(self):
        """The false positive the whole-universe scan caught.

        ALTBTC and TOTAL3BTC publish ONE computed value per day as all four
        OHLC fields with volume 0. That is byte-identical to an injected
        candle, and both feeds are alive and updating daily. A fabricated bar
        is only recognisable relative to real bars in the same series, so a
        series that is entirely this shape falls back to the date test.

        The symbol argument is load-bearing, not decoration. Granting the
        exemption on the strength of the bars alone — every row flat, therefore
        assume that is the encoding — would exempt a series that is fabrication
        from its first row too.
        The routing map answers now, so this test passes ALTBTC and the inverse
        case (identical bytes, undeclared symbol, must be caught) lives in
        tests/test_derived_series_exemption.py.
        """
        from carrac.precompute import _is_history_stale

        vals = [5.0735e-06, 5.1198e-06, 5.0967e-06]
        df = pd.DataFrame(
            {"open": vals, "high": vals, "low": vals, "close": vals,
             "volume": [0, 0, 0]},
            index=pd.date_range(end=pd.Timestamp(NOW.date()), periods=3, freq="D"),
        )
        assert _is_history_stale(df, 'ALTBTC', now=NOW) is False

    def test_a_zero_volume_bar_that_actually_moved_is_still_real(self):
        """Guard the other direction: zero reported volume is NOT enough.

        Some venues report 0 volume on a bar that genuinely traded a range.
        Only a bar that is BOTH flat and volumeless is synthetic — dropping
        every zero-volume bar would start flagging live assets as dead.
        """
        from carrac.precompute import _is_history_stale

        df = pd.DataFrame(
            {"open": [100.0, 101.0, 102.0], "high": [103.0, 104.0, 105.0],
             "low": [99.0, 100.0, 101.0], "close": [101.0, 102.0, 103.0],
             "volume": [0.0, 0.0, 0.0]},
            index=pd.date_range(end=pd.Timestamp(NOW.date()), periods=3, freq="D"),
        )
        assert _is_history_stale(df, 'BTC', now=NOW) is False


class TestSpliceIsRejectedOnStaleHistory:
    @pytest.fixture
    def db(self, monkeypatch):
        from database import OHLCVDatabase
        inst = OHLCVDatabase.__new__(OHLCVDatabase)
        inst.price_injection_rejects = set()
        return inst

    def test_stale_symbol_gets_no_synthetic_bar(self, db, monkeypatch):
        monkeypatch.setattr(db, "get_ohlcv_data", lambda s: _frozen_flatline(),
                            raising=False)
        out = db.get_multi_timeframe_data_with_realtime("MKR", realtime_price=1305.92)

        daily = out["1D"]
        assert 1305.92 not in list(daily["close"]), \
            "live price was spliced onto 57-day-old flatline history"
        assert daily.index.max() == pd.Timestamp("2026-06-09"), \
            "series should still end at the last REAL bar"
        assert "MKR" in db.price_injection_rejects

    def test_fresh_symbol_still_gets_its_live_price(self, db, monkeypatch):
        monkeypatch.setattr(db, "get_ohlcv_data", lambda s: _fresh_series(),
                            raising=False)
        out = db.get_multi_timeframe_data_with_realtime("BTC", realtime_price=63500.0)

        assert 63500.0 in list(out["1D"]["close"]), \
            "the feature broke — live prices must still reach collecting assets"
        assert "BTC" not in db.price_injection_rejects

    def test_implausible_price_is_still_rejected(self, db, monkeypatch):
        """The original RNDR guard must survive the new one being added."""
        monkeypatch.setattr(db, "get_ohlcv_data", lambda s: _fresh_series(),
                            raising=False)
        out = db.get_multi_timeframe_data_with_realtime("RNDR", realtime_price=7.03)

        assert 7.03 not in list(out["1D"]["close"])
        assert "RNDR" in db.price_injection_rejects
