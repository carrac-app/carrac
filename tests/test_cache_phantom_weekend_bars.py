"""Check D: weekend bars on an instrument that has no weekend.

Seven weekday-only symbols in production (GOLD, SILVER, OIL, COPPER, NATGAS,
PLATINUM, DXY) carry a bar dated EVERY Sunday from some date onward. Upstream
has none of them. GOLD has 3681 rows and 11 weekend bars, so the class started
on a date; it is not historical noise.

It matters because the phantom becomes Monday's predecessor:

    stored:  Sun 08-02 C=4122.10 -> Mon 08-03 C=4033.70  =  -2.14%
    real:    Fri 07-31 C=4049.10 -> Mon 08-03 C=4033.70  =  -0.38%

and the span note cannot catch it — Sun->Mon is one calendar day, so
it prints a bare "(1 bar)".

NEITHER EXISTING GUARD CAN SEE THIS, structurally:
  * _missing_business_days uses pd.bdate_range, which SKIPS weekends, so a
    SURPLUS weekend bar is invisible to it by construction;
  * this script's Check C already notices weekend bars — it excludes them from
    the voter cohort (see the comment at recent_session_gaps) — but only ever
    used that to decide who may vote. It never reported them.

Detection is by the series' own shape rather than a symbol taxonomy, so it
also covers dynamic symbols the script knows nothing about.
"""
import datetime

from scripts.cache_integrity_check import phantom_weekend_bars


def _weekdays(start, n):
    """n consecutive business days from `start`."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def _every_day(start, n):
    return [start + datetime.timedelta(days=i) for i in range(n)]


class TestPhantomWeekendBars:
    def test_clean_weekday_series_is_silent(self):
        dates = _weekdays(datetime.date(2026, 1, 5), 200)
        assert phantom_weekend_bars({'GOLD': dates}) == []

    def test_crypto_trades_every_day_and_is_not_flagged(self):
        """The false positive that would make this useless — 2/7 of a crypto
        series is a weekend and every one of those bars is real."""
        dates = _every_day(datetime.date(2026, 1, 1), 300)
        assert phantom_weekend_bars({'BTC': dates}) == []

    def test_a_weekday_series_with_sunday_bars_is_flagged(self):
        """The real GOLD signature: weekday series + one Sunday a week."""
        dates = _weekdays(datetime.date(2026, 1, 5), 200)
        sundays = [d for d in _every_day(datetime.date(2026, 5, 24), 80)
                   if d.weekday() == 6]
        out = phantom_weekend_bars({'GOLD': sorted(dates + sundays)})
        assert len(out) == 1
        assert 'GOLD' in out[0]
        assert 'WEEKEND-' in out[0] and 'BAR' in out[0]

    def test_the_finding_names_dates_so_it_can_be_checked(self):
        dates = _weekdays(datetime.date(2026, 1, 5), 200)
        phantom = datetime.date(2026, 8, 2)   # a Sunday
        assert phantom.weekday() == 6
        out = phantom_weekend_bars({'OIL': sorted(dates + [phantom])})
        assert '2026-08-02' in out[0]

    def test_short_series_is_not_judged(self):
        """A bootstrapping cache has too few rows for the ratio to mean
        anything — staying quiet beats a wrong alarm nobody will read."""
        dates = _weekdays(datetime.date(2026, 8, 3), 4)
        phantom = datetime.date(2026, 8, 2)
        assert phantom_weekend_bars({'NEW': sorted(dates + [phantom])}) == []

    def test_saturday_bars_count_too(self):
        dates = _weekdays(datetime.date(2026, 1, 5), 200)
        saturdays = [d for d in _every_day(datetime.date(2026, 5, 23), 80)
                     if d.weekday() == 5]
        out = phantom_weekend_bars({'GT': sorted(dates + saturdays)})
        assert len(out) == 1

    def test_multiple_symbols_each_get_a_finding(self):
        dates = _weekdays(datetime.date(2026, 1, 5), 200)
        sunday = datetime.date(2026, 8, 2)
        out = phantom_weekend_bars({
            'GOLD': sorted(dates + [sunday]),
            'SILVER': sorted(dates + [sunday]),
            'BTC': _every_day(datetime.date(2026, 1, 1), 300),
        })
        assert len(out) == 2
        assert {'GOLD', 'SILVER'} == {f.split(':')[0] for f in out}

    def test_empty_input_is_safe(self):
        assert phantom_weekend_bars({}) == []
        assert phantom_weekend_bars({'X': []}) == []


class TestRecurrenceSurvivesAlertDedup:
    """The writer adds a bar EVERY weekend, so recurrence is the
    signal, and it must not be deduplicated into silence.

    ``main()`` keys alert state on ``SYMBOL:KIND``, where KIND is the first
    word after the colon. With a bare ``WEEKEND`` kind the first alert fires,
    the key is recorded, and every subsequent phantom bar collapses onto the
    same key — reported once, then silent forever while the fault continues.
    Folding the newest offending date into the kind makes next weekend's bar a
    genuinely new key.
    """

    @staticmethod
    def _key_of(finding):
        """Verbatim copy of main()'s dedup rule — this is the thing under
        test, so it must not be imported-and-trusted differently here."""
        symbol, _, rest = finding.partition(':')
        return f"{symbol}:{rest.strip().split(' ')[0]}"

    def _findings_for(self, sundays):
        dates = _weekdays(datetime.date(2026, 1, 5), 200)
        return phantom_weekend_bars({'GOLD': sorted(dates + sundays)})

    def test_a_new_phantom_bar_next_weekend_is_a_new_alert_key(self):
        week1 = datetime.date(2026, 8, 2)
        week2 = datetime.date(2026, 8, 9)
        assert week1.weekday() == 6 and week2.weekday() == 6

        k1 = self._key_of(self._findings_for([week1])[0])
        k2 = self._key_of(self._findings_for([week1, week2])[0])
        assert k1 != k2, (
            'a second phantom bar reused the first alert key — every weekend '
            'after the first would be silent'
        )

    def test_an_unchanged_series_keeps_the_same_key(self):
        """The flip side: a fault that has NOT recurred must not re-alert
        every day. Otherwise the guard becomes noise and trains you to ignore
        it."""
        sunday = datetime.date(2026, 8, 2)
        k1 = self._key_of(self._findings_for([sunday])[0])
        k2 = self._key_of(self._findings_for([sunday])[0])
        assert k1 == k2

    def test_the_key_carries_the_newest_date_not_the_oldest(self):
        old, new = datetime.date(2026, 5, 24), datetime.date(2026, 8, 2)
        key = self._key_of(self._findings_for([old, new])[0])
        assert key.endswith(str(new)), key
