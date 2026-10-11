"""The briefing must not call a multi-day move a 1-bar move.

When commodity/FX caches miss a bar, bar-over-bar spans two days. The free
public /brief must not publish "Gold surged +5.6% today to $4,326" off such a
pair when gold moved only +0.72% that day.

The span is stated as a FACT rather than guarding on a threshold: telling
a real hole from a weekend or a public holiday needs a trading calendar this
codebase does not have, and a naive day-count would fire every holiday until
it was ignored. Printing the two dates costs nothing, has no
false-positive mode, and makes "today" unwritable.

Bars below are the REAL prod values from /app/data/GOLD_daily.csv.
"""
import calendar
import datetime

from morning_engine.data_assembly import _format_asset_tf


def _ts(iso):
    """UTC midnight unix timestamp for an ISO date."""
    d = datetime.date.fromisoformat(iso)
    return calendar.timegm(datetime.datetime(d.year, d.month, d.day).timetuple())


def _bar(iso, close, o=None, h=None, low=None):
    c = float(close)
    return {"time": _ts(iso), "open": o if o is not None else c,
            "high": h if h is not None else c, "low": low if low is not None else c,
            "close": c}


def _chg_line(bars, tf="1D"):
    out = _format_asset_tf({"ohlcv": bars}, "GOLD", tf)
    return next(ln for ln in out.splitlines() if ln.startswith("Price:"))


# --- the multi-day-span case ------------------------------------------------

def test_gold_2026_08_06_declares_the_two_day_span():
    """The exact bar pair behind 'Gold surged +5.6% today'."""
    bars = [
        _bar("2026-07-30", 4100.10009765625),
        _bar("2026-07-31", 4049.10009765625),
        _bar("2026-08-02", 4122.10009765625),
        _bar("2026-08-03", 4033.699951171875),
        _bar("2026-08-04", 4095.39990234375),
        _bar("2026-08-06", 4326.39990234375, o=4307.0, h=4341.10009765625, low=4304.89990234375),
    ]
    line = _chg_line(bars)
    assert "+5.6% (1 bar" in line, line          # the number itself is unchanged
    assert "2026-08-04->2026-08-06 spans 2 days" in line, line


def test_consecutive_days_stay_clean():
    """The common case must not gain noise."""
    bars = [_bar("2026-08-04", 100.0), _bar("2026-08-05", 101.0), _bar("2026-08-06", 102.0)]
    line = _chg_line(bars)
    assert "(1 bar)" in line, line
    assert "spans" not in line, line


def test_weekend_reads_honestly_rather_than_as_an_error():
    """Fri -> Mon is legitimate; it is described, not flagged."""
    bars = [_bar("2026-07-30", 100.0), _bar("2026-07-31", 100.0), _bar("2026-08-03", 103.0)]
    line = _chg_line(bars)
    assert "2026-07-31->2026-08-03 spans 3 days" in line, line


def test_single_bar_series_has_no_span_note():
    """prev is last — a self-comparison must not claim a span."""
    line = _chg_line([_bar("2026-08-06", 4326.4)])
    assert "spans" not in line, line


def test_malformed_time_does_not_break_the_block():
    """A bad timestamp drops the note; it must never lose the whole asset."""
    bars = [_bar("2026-08-04", 100.0), _bar("2026-08-06", 105.0)]
    bars[0]["time"] = "not-a-timestamp"
    line = _chg_line(bars)
    assert "+5.0% (1 bar" in line, line


# --- weekly bars must not carry a span note (fix 4: every weekly row is
# ~7 days apart BY CONSTRUCTION — that is not a data hole, it is what
# "weekly" means, so the span note is pure noise there) --------

def test_weekly_bars_carry_no_span_note():
    """Weekly bars are always ~7 days apart — that must never be reported
    as a span (it would fire on literally every weekly row)."""
    bars = [_bar("2026-07-20", 100.0), _bar("2026-07-27", 101.0), _bar("2026-08-03", 105.0)]
    line = _chg_line(bars, tf="1W")
    assert "spans" not in line, line


def test_daily_span_note_unaffected_by_the_weekly_scoping():
    """The weekly scoping must not weaken the daily-bar guard it was scoped
    away from — this is the same fixture as the multi-day-span case, tf=1D."""
    bars = [
        _bar("2026-07-30", 4100.10009765625),
        _bar("2026-07-31", 4049.10009765625),
        _bar("2026-08-02", 4122.10009765625),
        _bar("2026-08-03", 4033.699951171875),
        _bar("2026-08-04", 4095.39990234375),
        _bar("2026-08-06", 4326.39990234375, o=4307.0, h=4341.10009765625, low=4304.89990234375),
    ]
    line = _chg_line(bars, tf="1D")
    assert "2026-08-04->2026-08-06 spans 2 days" in line, line


def test_five_bar_figure_is_untouched():
    """Only the 1-bar label was wrong; the 5-bar one matched reality."""
    bars = [
        _bar("2026-07-30", 4100.10009765625), _bar("2026-07-31", 4049.10009765625),
        _bar("2026-08-02", 4122.10009765625), _bar("2026-08-03", 4033.699951171875),
        _bar("2026-08-04", 4095.39990234375), _bar("2026-08-06", 4326.39990234375),
    ]
    # 4326.3999 vs ohlcv[-6] = 4100.1001 -> +5.5%, as published and correct
    assert "+5.5% (5 trading bars)" in _chg_line(bars)
