"""Check E: a derived series that quietly stopped advancing.

TOTAL / TOTAL2 / TOTAL3 / BTC.D / USDT.D / OTHERS.D (and the *BTC ratios) are
recomputed over a rolling window from top-coin market caps. When the upstream
fetch fails, ``database.py::_get_crypto_metric_ohlcv`` serves the last-good CSV
and logs nothing louder than::

    logger.warning('API refresh failed for {symbol}, using stale cache')

The chart keeps rendering and simply stops moving — no error, no banner, no
staleness marker. A visitor sees a chart that looks fine.

NO EXISTING CHECK CAN SEE THIS, structurally — the file is internally perfect:
  * Check A (interior gaps) and Check C (one-session holes) look BETWEEN rows;
  * Check B compares row COUNTS against a high-water mark, and a frozen file
    keeps every row it had;
  * Check D looks for a SURPLUS weekend bar.
A stale TAIL is invisible to all four, so Check E exists for it.

The threshold is decidable only because these are 24/7 crypto aggregates with
a same-day cadence — which is why Check E is scoped to them and not widened to
every cache (ordinary symbols have exchange calendars and listing gaps, and
need Check C's cohort vote instead).
"""
import datetime

from scripts.cache_integrity_check import (
    DERIVED_SERIES,
    DERIVED_STALE_DAYS,
    frozen_derived_series,
)

TODAY = datetime.date(2026, 8, 8)


def _series(last_offset_days, n=366, today=TODAY):
    """`n` consecutive daily bars ending `last_offset_days` before `today`."""
    end = today - datetime.timedelta(days=last_offset_days)
    return [end - datetime.timedelta(days=i) for i in range(n - 1, -1, -1)]


class TestFrozenDerivedSeriesIsCaught:
    def test_a_frozen_total_is_reported(self):
        findings = frozen_derived_series({'TOTAL': _series(21)}, today=TODAY)
        assert len(findings) == 1
        assert findings[0].startswith('TOTAL: FROZEN')
        assert '21d old' in findings[0]

    def test_every_derived_series_is_covered_not_just_the_six_named(self):
        """The original report names six; the same code path feeds eleven. A sibling
        sweep that stops at the reported symbols leaves the rest silent."""
        frozen = {sym: _series(21) for sym in DERIVED_SERIES}
        findings = frozen_derived_series(frozen, today=TODAY)
        assert len(findings) == len(DERIVED_SERIES)

    def test_dominance_symbols_use_their_on_disk_names(self):
        """The '.' in BTC.D becomes '_' in the filename, and the scanner keys
        off the filename. A set written as 'BTC.D' would silently match
        nothing — the guard would exist and cover zero symbols."""
        assert {'BTC_D', 'USDT_D', 'OTHERS_D'} <= DERIVED_SERIES
        assert not any('.' in s for s in DERIVED_SERIES)


class TestCheckEDoesNotCryWolf:
    """A guard left red becomes noise, and then it trains you to ignore
    guards."""

    def test_a_current_series_is_silent(self):
        assert frozen_derived_series({'TOTAL': _series(0)}, today=TODAY) == []

    def test_exactly_at_the_limit_is_silent(self):
        """The derived series legitimately lags its constituents by up to a
        day, so the boundary must not fire."""
        series = {'TOTAL': _series(DERIVED_STALE_DAYS)}
        assert frozen_derived_series(series, today=TODAY) == []

    def test_one_day_past_the_limit_fires(self):
        series = {'TOTAL': _series(DERIVED_STALE_DAYS + 1)}
        assert len(frozen_derived_series(series, today=TODAY)) == 1

    def test_a_frozen_ORDINARY_symbol_is_not_reported(self):
        """BTC has an upstream of its own and its own freshness handling.
        Check E claims only the derived series — widening it here without
        deciding a threshold per class is what was warned against."""
        assert frozen_derived_series({'BTC': _series(30)}, today=TODAY) == []

    def test_an_empty_series_is_not_reported_as_frozen(self):
        """An empty file is Check A's finding ('EMPTY cache file'), not a
        freeze — double-reporting one fault as two is noise."""
        assert frozen_derived_series({'TOTAL': []}, today=TODAY) == []


class TestFindingShape:
    def test_the_finding_key_is_distinct_from_other_checks(self):
        """main() de-duplicates alerts by 'SYMBOL:KIND', where KIND is the
        first word after the colon. Colliding with GAP/ROWS/WEEKEND would make
        a freeze silently inherit another finding's alert state."""
        finding = frozen_derived_series({'TOTAL': _series(21)}, today=TODAY)[0]
        symbol, _, rest = finding.partition(':')
        assert symbol == 'TOTAL'
        assert rest.strip().split(' ')[0] == 'FROZEN'
