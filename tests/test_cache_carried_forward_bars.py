"""Check G: backfilled bars that copy the previous session's volume.

The late-backfill writer fills yesterday's hole with a bar whose OHLC is
plausible but whose VOLUME is an exact copy of the previous session's. Measured
in production over every volume-bearing weekday cache (16 series, ~14 years): three
dates carry that signature on 8-9 instruments at once —

    2024-09-09 (Mon)  n=9   COPPER,CORN,GOLD,NATGAS,OIL,PLATINUM,SILVER,SOYBEANS,WHEAT
    2025-10-13 (Mon)  n=9   same set
    2026-08-10 (Mon)  n=8   same minus PLATINUM (its volume went to 0)

while every other volume repeat in history is a 1-2 instrument coincidence
(225 repeat-dates total; only one other date, 2017-08-30, clusters at n=3 —
GOLD/OIL/SILVER, which predates Carrac's own collection and is plausibly the
same fault upstream).

So the predicate is the CLUSTER, not the repeat: a volume repeat on >= 3
weekday instruments dated the same day is a backfill event, not a market.

Deliberately NOT conditioned on a weekend/holiday gap: the rotating fill set
means a Tuesday hole gets filled with Monday's volume at gap=1 (observed on real data:
CORN/WHEAT/SOYBEANS missing a Tuesday at 06:20 with Monday
already carrying Friday's volume). A gap predicate would miss every mid-week
fill.

STATED BLIND SPOT: instruments publishing volume 0 throughout (FX pairs,
PLATINUM, DXY) are unreadable by any volume test. Their half of a fill day is
visible only as the Check C MISSING finding the morning before it is filled.
"""
import datetime

from scripts.cache_integrity_check import (
    CARRIED_MIN_INSTRUMENTS,
    carried_forward_bars,
)


def _weekday_series(start, n, base_vol=1000.0):
    """n business-day (date, volume) rows with distinct volumes."""
    out, d, i = [], start, 0
    while len(out) < n:
        if d.weekday() < 5:
            out.append((d, base_vol + i))
            i += 1
        d += datetime.timedelta(days=1)
    return out


def _with_repeat_on(rows, date):
    """Copy the previous row's volume onto the row at `date`."""
    out = []
    for i, (d, v) in enumerate(rows):
        if d == date and i > 0:
            out.append((d, out[i - 1][1]))
        else:
            out.append((d, v))
    return out


START = datetime.date(2026, 1, 5)   # a Monday
MONDAY = datetime.date(2026, 8, 10)  # the live fault date
TUESDAY = datetime.date(2026, 8, 11)


class TestCarriedForwardBars:
    def test_three_instruments_sharing_a_repeat_date_is_flagged(self):
        rows = _weekday_series(START, 200)
        vols = {s: _with_repeat_on(rows, MONDAY)
                for s in ("GOLD", "OIL", "SILVER")}
        out = carried_forward_bars(vols)
        assert len(out) == 1
        assert "CARRIED-2026-08-10" in out[0]
        for s in ("GOLD", "OIL", "SILVER"):
            assert s in out[0]

    def test_two_instruments_is_a_coincidence_and_stays_silent(self):
        """225 repeat-dates exist in production history; all but four are 1-2
        instruments. Firing on those would be noise from day one."""
        rows = _weekday_series(START, 200)
        vols = {s: _with_repeat_on(rows, MONDAY) for s in ("GOLD", "SILVER")}
        assert carried_forward_bars(vols) == []
        assert CARRIED_MIN_INSTRUMENTS == 3

    def test_clean_series_are_silent(self):
        vols = {s: _weekday_series(START, 200) for s in ("GOLD", "OIL", "SPX")}
        assert carried_forward_bars(vols) == []

    def test_zero_volume_repeats_are_unreadable_not_faults(self):
        """PLATINUM/DXY/FX publish volume 0 on every row — 0 == 0 forever.
        Treating that as a repeat would flag them daily."""
        rows = [(d, 0.0) for d, _ in _weekday_series(START, 200)]
        vols = {s: rows for s in ("EURUSD", "DXY", "PLATINUM", "USDJPY")}
        assert carried_forward_bars(vols) == []

    def test_a_247_series_never_votes(self):
        """Crypto volumes are real on weekends too; the fault class is the
        weekday-instrument backfill writer. A crypto repeat coincidence must
        not help two commodities across the n>=3 line."""
        wk = _weekday_series(START, 200)
        crypto = [(START + datetime.timedelta(days=i), 500.0 + i)
                  for i in range(200)]
        crypto = _with_repeat_on(crypto, MONDAY)
        vols = {
            "GOLD": _with_repeat_on(wk, MONDAY),
            "SILVER": _with_repeat_on(wk, MONDAY),
            "BTC": crypto,
        }
        assert carried_forward_bars(vols) == []

    def test_short_series_is_not_judged(self):
        rows = _weekday_series(datetime.date(2026, 7, 1), 10)
        vols = {s: _with_repeat_on(rows, datetime.date(2026, 7, 13))
                for s in ("A", "B", "C")}
        assert carried_forward_bars(vols) == []

    def test_a_midweek_fill_at_gap_1_still_fires(self):
        """The rotating-set case: a Tuesday hole filled with Monday's volume.
        Any weekend/holiday-gap predicate misses this by construction."""
        rows = _weekday_series(START, 200)
        vols = {s: _with_repeat_on(rows, TUESDAY)
                for s in ("CORN", "WHEAT", "SOYBEANS")}
        out = carried_forward_bars(vols)
        assert len(out) == 1
        assert "CARRIED-2026-08-11" in out[0]

    def test_each_fault_date_is_its_own_finding(self):
        rows = _weekday_series(datetime.date(2024, 1, 1), 700)
        d1, d2 = datetime.date(2024, 9, 9), datetime.date(2025, 10, 13)
        vols = {s: _with_repeat_on(_with_repeat_on(rows, d1), d2)
                for s in ("GOLD", "OIL", "SILVER", "COPPER")}
        out = carried_forward_bars(vols)
        assert len(out) == 2
        assert any("CARRIED-2024-09-09" in f for f in out)
        assert any("CARRIED-2025-10-13" in f for f in out)

    def test_empty_input_is_safe(self):
        assert carried_forward_bars({}) == []
        assert carried_forward_bars({"X": []}) == []


class TestRecurrenceSurvivesAlertDedup:
    """The fill happens DAILY on a rotating set, so each fault date must be a
    genuinely new alert key — the Check D lesson, applied here from the start.
    """

    @staticmethod
    def _key_of(finding):
        """Verbatim copy of main()'s dedup rule — the thing under test."""
        symbol, _, rest = finding.partition(":")
        return f"{symbol}:{rest.strip().split(' ')[0]}"

    def _finding_for(self, date):
        rows = _weekday_series(START, 200)
        vols = {s: _with_repeat_on(rows, date)
                for s in ("GOLD", "OIL", "SILVER")}
        return carried_forward_bars(vols)[0]

    def test_a_new_fill_date_is_a_new_alert_key(self):
        k1 = self._key_of(self._finding_for(MONDAY))
        k2 = self._key_of(self._finding_for(TUESDAY))
        assert k1 != k2, (
            "a second fill date reused the first alert key — every fill after "
            "the first would be silent while the fault recurs daily"
        )

    def test_the_same_fault_date_keeps_a_stable_key(self):
        assert (self._key_of(self._finding_for(MONDAY))
                == self._key_of(self._finding_for(MONDAY)))


class TestTheMeasuredProdSignature:
    """Acceptance: the three known dates, at the instrument counts measured on
    prod.
    """

    KNOWN = {
        datetime.date(2024, 9, 9): ["COPPER", "CORN", "GOLD", "NATGAS", "OIL",
                                    "PLATINUM", "SILVER", "SOYBEANS", "WHEAT"],
        datetime.date(2025, 10, 13): ["COPPER", "CORN", "GOLD", "NATGAS", "OIL",
                                      "PLATINUM", "SILVER", "SOYBEANS", "WHEAT"],
        datetime.date(2026, 8, 10): ["COPPER", "CORN", "GOLD", "NATGAS", "OIL",
                                     "SILVER", "SOYBEANS", "WHEAT"],
    }

    def test_all_three_known_dates_fire_with_full_instrument_lists(self):
        base = _weekday_series(datetime.date(2024, 1, 1), 700)
        vols = {}
        for date, syms in self.KNOWN.items():
            for s in syms:
                vols[s] = _with_repeat_on(vols.get(s, base), date)
        out = carried_forward_bars(vols)
        assert len(out) == 3
        for date, syms in self.KNOWN.items():
            match = [f for f in out if f"CARRIED-{date}" in f]
            assert len(match) == 1, f"{date} did not fire"
            for s in syms:
                assert s in match[0], f"{s} missing from the {date} finding"
