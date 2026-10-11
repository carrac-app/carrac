"""Check F: any symbol that quietly stopped collecting.

A symbol's chart can sit frozen for weeks with nothing telling anyone.
The app KNOWS — ``_is_history_stale`` computes it per asset and the UI publishes
an honest "no data since X, so no readings are published" block — but the only
ALARM, ``carrac/precompute.py::_report_stale_assets``, fires solely when
``len(newly) >= _STALE_ALARM_JUMP`` (default 3) in a SINGLE cycle, and
``prev_count is None`` suppresses the first cycle after every restart. **One
symbol dying alone never alarms.**

Checks A–E cannot see it either: A and C look BETWEEN rows, B counts rows, D
looks for surplus weekend bars, E is scoped to the eleven derived series.

Run against a live prod data dir this found **11 dead symbols**
across 376 caches, with zero false positives.

The threshold is self-calibrating: each series is judged against its own median
inter-bar gap, so there is no asset-class map to keep in sync. The one thing
the median cannot see is a market HOLIDAY, which is why the floor is chosen
from the series' own weekend behaviour.
"""
import datetime

from scripts.cache_integrity_check import (
    STALE_CEILING_DAYS,
    STALE_FLOOR_247_DAYS,
    STALE_FLOOR_CALENDAR_DAYS,
    frozen_series,
)

TODAY = datetime.date(2026, 8, 8)


def _daily(n, end_offset=0, today=TODAY):
    """A 24/7 series: every calendar day."""
    end = today - datetime.timedelta(days=end_offset)
    return [end - datetime.timedelta(days=i) for i in range(n - 1, -1, -1)]


def _weekdays(n, end_offset=0, today=TODAY):
    """A weekday-only series, weekends genuinely absent."""
    out, d = [], today - datetime.timedelta(days=end_offset)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return sorted(out)


class TestFrozenSeriesDetection:
    def test_ton_39_days_frozen_is_caught(self):
        findings = frozen_series({'TON': _daily(300, end_offset=39)}, today=TODAY)
        assert len(findings) == 1
        assert findings[0].startswith('TON: STALE')
        assert '39d old' in findings[0]

    def test_a_current_series_is_silent(self):
        assert frozen_series({'BTC': _daily(300)}, today=TODAY) == []


class TestHolidayImmunityForCalendarInstruments:
    """The false positive that would make this useless. Equities, FX and
    commodities all calibrate to cadence 1.0 — weekend gaps are a MINORITY of
    gaps, so the median is 1 — which would put their limit at 4 days. Golden
    Week, Lunar New Year and Christmas all run longer than that, and on the day
    that fired it would flag every equity at once."""

    def test_a_long_exchange_holiday_does_not_flag(self):
        # 9 days dark — a Golden Week / Lunar New Year shaped closure.
        series = {'NIKKEI': _weekdays(300, end_offset=9)}
        assert frozen_series(series, today=TODAY) == [], \
            'a routine exchange holiday would have paged as a dead feed'

    def test_a_normal_weekend_does_not_flag(self):
        assert frozen_series({'SPX': _weekdays(300, end_offset=3)}, today=TODAY) == []

    def test_a_genuinely_dead_calendar_instrument_is_still_caught(self):
        series = {'BDX': _weekdays(300, end_offset=44)}
        findings = frozen_series(series, today=TODAY)
        assert len(findings) == 1
        assert 'follows an exchange calendar' in findings[0]

    def test_the_two_floors_are_distinguishable(self):
        """A 24/7 series must be judged faster than a calendar one, or the
        holiday allowance silently slows crypto detection to 10 days too."""
        assert STALE_FLOOR_247_DAYS < STALE_FLOOR_CALENDAR_DAYS

    def test_a_crypto_series_gets_the_tight_floor(self):
        gap = STALE_FLOOR_247_DAYS + 1
        findings = frozen_series({'SOL': _daily(300, end_offset=gap)}, today=TODAY)
        assert len(findings) == 1
        assert 'trades weekends' in findings[0]

    def test_the_same_gap_is_forgiven_on_a_calendar_instrument(self):
        """Same age, opposite verdict — this is the whole point of two floors."""
        gap = STALE_FLOOR_247_DAYS + 1
        assert frozen_series({'DAX': _weekdays(300, end_offset=gap)}, today=TODAY) == []


class TestSelfCalibration:
    def test_a_weekly_series_is_judged_against_its_own_cadence(self):
        """A 7-day-cadence series 9 days old is barely late, not dead. A fixed
        daily threshold would flag it every single week."""
        weekly = [TODAY - datetime.timedelta(days=9 + 7 * i) for i in range(60)]
        assert frozen_series({'WEEKLYISH': sorted(weekly)}, today=TODAY) == []

    def test_a_weekly_series_that_actually_dies_is_caught(self):
        weekly = [TODAY - datetime.timedelta(days=90 + 7 * i) for i in range(60)]
        assert len(frozen_series({'WEEKLYISH': sorted(weekly)}, today=TODAY)) == 1

    def test_slow_death_cannot_raise_the_bar_indefinitely(self):
        """The known weakness of self-calibration: a series whose gaps widen
        drifts its own threshold up. STALE_MAX_CADENCE_DAYS and the ceiling
        bound it, or a dying feed could excuse itself forever."""
        sparse = [TODAY - datetime.timedelta(days=200 + 30 * i) for i in range(40)]
        findings = frozen_series({'DYING': sorted(sparse)}, today=TODAY)
        assert len(findings) == 1, 'a 30-day-cadence series 200d old must still flag'

    def test_nothing_outlives_the_ceiling(self):
        sparse = [TODAY - datetime.timedelta(days=STALE_CEILING_DAYS + 5 + 40 * i)
                  for i in range(40)]
        assert len(frozen_series({'ANCIENT': sorted(sparse)}, today=TODAY)) == 1


class TestNoiseControl:
    def test_derived_series_are_left_to_check_e(self):
        """Check E already owns them with a tighter fixed rule. Two findings
        for one fault is noise."""
        assert frozen_series({'TOTAL': _daily(300, end_offset=39)}, today=TODAY) == []

    def test_a_short_series_is_not_judged(self):
        """A bootstrapping cache has too few bars to measure a cadence from."""
        assert frozen_series({'NEW': _daily(5, end_offset=30)}, today=TODAY) == []

    def test_empty_input_is_safe(self):
        assert frozen_series({}, today=TODAY) == []
        assert frozen_series({'X': []}, today=TODAY) == []

    def test_the_finding_key_is_stable_as_the_series_ages(self):
        """main() dedups on SYMBOL:KIND. A frozen series is ONE ongoing fault,
        unlike Check D's phantom bars where each week is a NEW fault — so the
        key must NOT carry a date, or it would re-alert every single day as the
        age ticks up."""
        def key(f):
            sym, _, rest = f.partition(':')
            return f"{sym}:{rest.strip().split(' ')[0]}"

        day1 = frozen_series({'TON': _daily(300, end_offset=39)}, today=TODAY)[0]
        day2 = frozen_series({'TON': _daily(300, end_offset=40)}, today=TODAY)[0]
        assert key(day1) == key(day2) == 'TON:STALE'


class TestOnlyTrackedSymbolsAreJudged:
    """Measured on real data: of 12 frozen caches, only THREE belonged to
    symbols the app still tracks. The other nine were leftover files for
    retired symbols — they will never advance and nobody can fix them, so
    paging about them is unactionable by construction and would bury the three
    that matter.

    The manifest is written by
    ``carrac/precompute.py::_write_tracked_symbols_manifest`` because this
    script is stdlib-only (it must run when the app is down) and so cannot ask
    the app what it tracks.
    """

    def test_an_untracked_symbol_is_ignored(self):
        frozen = {'BDX': _daily(300, end_offset=44)}
        assert frozen_series(frozen, today=TODAY, tracked={'BTC', 'TON'}) == []

    def test_a_tracked_symbol_is_still_reported(self):
        frozen = {'TON': _daily(300, end_offset=39)}
        assert len(frozen_series(frozen, today=TODAY, tracked={'BTC', 'TON'})) == 1

    def test_no_manifest_means_judge_everything(self):
        """``tracked=None`` is the in-function contract for 'caller could not
        supply a universe'. scan() refuses to call it in that state at all —
        see TestManifestLoading — but the function itself must not silently
        drop findings if some other caller passes None."""
        frozen = {'BDX': _daily(300, end_offset=44)}
        assert len(frozen_series(frozen, today=TODAY, tracked=None)) == 1


class TestASyntheticTailCannotHideADeadSeries:
    """Check F reads the newest bar's DATE, and nothing else.

    Real bars stop, then OHLC-flat, zero-volume rows are appended on top, one
    per day. Check F sees a recent series and never fires.

    ``scan()`` is where the blindness lives — it parses column 0 and discards
    every other field, so ``frozen_series`` cannot judge what it never receives.
    These tests therefore go through ``scan()``, not around it.
    """

    @staticmethod
    def _write(tmp_path, symbol, rows):
        """rows: list of (days_ago, o, h, l, c, v)."""
        today = datetime.date.today()
        lines = ['timestamp,open,high,low,close,volume']
        for days_ago, o, h, lo, c, v in sorted(rows, key=lambda r: -r[0]):
            d = today - datetime.timedelta(days=days_ago)
            lines.append(f'{d.isoformat()},{o},{h},{lo},{c},{v}')
        (tmp_path / f'{symbol}_daily.csv').write_text('\n'.join(lines) + '\n')

    @staticmethod
    def _manifest(tmp_path, symbols):
        import json
        (tmp_path / '_tracked_symbols.json').write_text(json.dumps({
            'symbols': symbols,
            'written_at': datetime.date.today().isoformat(),
        }))

    @staticmethod
    def _real(days_ago, price):
        """A bar that records trading: it has a range and it has volume."""
        return (days_ago, price, price * 1.02, price * 0.98, price * 1.01, 5000.0)

    @staticmethod
    def _synthetic(days_ago, price):
        """The shape ``inject_synthetic_candle`` produces: flat, no volume."""
        return (days_ago, price, price, price, price, 0.0)

    def _stale_findings(self, tmp_path):
        from scripts.cache_integrity_check import scan
        findings, _counts = scan(data_dir=str(tmp_path))
        return [f for f in findings if ': STALE' in f]

    def test_a_synthetic_tail_does_not_hide_a_dead_series(self, tmp_path):
        """In miniature: real bars stop, fabricated rows keep
        the newest DATE current, and the series reads as alive."""
        rows = [self._real(60 + i, 1.5) for i in range(40)]
        rows += [self._synthetic(i, 1.5) for i in range(60)]
        self._write(tmp_path, 'DEADCOIN', rows)
        self._manifest(tmp_path, ['DEADCOIN'])

        findings = self._stale_findings(tmp_path)

        assert len(findings) == 1, (
            'a series whose real bars stopped 60 days ago was reported healthy '
            'because 60 fabricated rows kept its newest date current'
        )
        assert findings[0].startswith('DEADCOIN: STALE')
        assert '60d old' in findings[0]
        # The alert has to explain why this was invisible, or the human reading
        # it replies "no it isn't, the chart updated this morning" — the file
        # genuinely does run to today.
        assert 'record no trading' in findings[0]

    def test_a_series_with_no_synthetic_tail_does_not_claim_one(self, tmp_path):
        """The masking clause must be earned. CHILLGUY, LRC and TON are dead
        with honestly-absent tails, and describing them as masked would be a
        fabrication of its own."""
        rows = [self._real(60 + i, 1.5) for i in range(40)]
        self._write(tmp_path, 'HONESTLYDEAD', rows)
        self._manifest(tmp_path, ['HONESTLYDEAD'])

        findings = self._stale_findings(tmp_path)

        assert len(findings) == 1
        assert 'record no trading' not in findings[0]

    def test_a_live_series_is_still_silent(self, tmp_path):
        """The control. A real feed carries one synthetic bar for the session in
        progress — that is normal and must not flag."""
        rows = [self._real(i, 1.5) for i in range(1, 100)]
        rows += [self._synthetic(0, 1.5)]
        self._write(tmp_path, 'LIVECOIN', rows)
        self._manifest(tmp_path, ['LIVECOIN'])

        assert self._stale_findings(tmp_path) == []

    def test_a_series_that_is_entirely_flat_is_not_declared_dead(self, tmp_path):
        """The load-bearing exemption, and the one no unit test caught the first
        time — the prod scan did. A fabricated bar is only recognisable RELATIVE
        to real ones. Derived ratio feeds publish ONE computed value a day as all
        four OHLC fields with volume 0 and are perfectly alive; ALTBTC moved
        5.0735e-06 -> 5.1199e-06 -> 5.0967e-06 across the last three days while
        looking synthetic on every row.

        **The original rationale here was that the exemption must hold on the
        bars alone, "not by a lookup that may not cover the next such feed", and
        this test used an undeclared symbol to enforce that. The reference-data rule
        reversed it deliberately** — see tests/test_derived_series_exemption.py
        for the full argument. Two things changed: the lookup is now derived from
        the routing map that decides how a series is FETCHED, so a feed cannot be
        collected as derived without being covered; and the price of inferring
        from bars was measured — a series that is all-synthetic from row one is
        permanently exempt from every guard, which is where AGIX spent 764 days.

        So Check F still does not call this STALE — the file genuinely is being
        updated, and saying "no new data" about it would be false. The verdict
        that this series records no trading at all is now its own finding.
        """
        # Every row flat and volumeless, but the VALUE advances daily — alive.
        flat = [(i, v, v, v, v, 0) for i, v
                in ((i, 5.0e-06 + i * 1e-09) for i in range(100))]
        self._write(tmp_path, 'RATIOFEED', flat)
        self._manifest(tmp_path, ['RATIOFEED'])

        assert self._stale_findings(tmp_path) == [], (
            'a series whose file is still being written was reported as having '
            'stopped collecting — that is a different (and false) claim'
        )

        from scripts.cache_integrity_check import scan
        findings, _counts = scan(data_dir=str(tmp_path))
        assert [f for f in findings if f.startswith('RATIOFEED: NO-REAL-BARS')], (
            'nothing in this series records trading and nothing declares it a '
            'computed feed, and the scan said nothing at all'
        )


class TestTheSyntheticRuleDoesNotDriftFromTheApp:
    """The sibling sweep — enforced rather than asserted.

    ``carrac.staleness.synthetic_bar_mask`` is the app's one definition of "this
    bar records no trading", shared by the read guard, the write guard and the
    purge script. This script CANNOT import it: it is stdlib-only by design so
    it still runs when the app is down, and the host has no pandas outside the
    container.

    So the rule is necessarily written twice, which is the precise setup that
    produced an earlier defect — "what counts as a real bar" decided in one place and
    assumed in another. This test is what keeps the second copy honest.
    """

    CASES = [
        ('flat and volumeless — the injected candle', 1.5, 1.5, 1.5, 1.5, 0.0, True),
        ('flat with real volume — a genuine no-range session', 1.5, 1.5, 1.5, 1.5, 900.0, False),
        ('moved with zero volume — a venue under-reporting', 1.0, 2.0, 0.5, 1.7, 0.0, False),
        ('an ordinary bar', 1.0, 1.1, 0.9, 1.05, 4200.0, False),
        ('integer zero volume, as ALTBTC writes it', 5e-06, 5e-06, 5e-06, 5e-06, 0, True),
        ('scientific notation, flat', 5.82e-06, 5.82e-06, 5.82e-06, 5.82e-06, 0.0, True),
        ('close differs in the last digit', 1.5, 1.5, 1.5, 1.5000001, 0.0, False),
    ]

    def test_the_two_implementations_agree_row_for_row(self):
        import pandas as pd

        from carrac.staleness import synthetic_bar_mask
        from scripts.cache_integrity_check import is_synthetic_row

        for label, o, h, low, c, v, expected in self.CASES:
            df = pd.DataFrame(
                [{'open': o, 'high': h, 'low': low, 'close': c, 'volume': v}],
                index=[pd.Timestamp('2026-08-08')],
            )
            canonical = bool(synthetic_bar_mask(df).iloc[0])
            stdlib = is_synthetic_row(str(o), str(h), str(low), str(c), str(v))

            assert canonical == expected, f'canonical rule changed: {label}'
            assert stdlib == canonical, (
                f'the monitor and the app disagree on: {label} '
                f'(app={canonical}, monitor={stdlib})'
            )

    def test_an_unparseable_row_is_never_called_synthetic(self):
        """Both sides must fail the same way. pandas coerces junk to NaN, and
        NaN == NaN is False, so an unreadable bar is not fabricated — it is
        unreadable. Declaring it fabricated would let a parse failure purge
        real history."""
        from scripts.cache_integrity_check import is_synthetic_row

        assert is_synthetic_row('n/a', 'n/a', 'n/a', 'n/a', '0') is False


class TestManifestLoading:
    """A stale manifest is more dangerous than a missing one: it would
    re-classify live symbols as retired and silently stop reporting them."""

    @staticmethod
    def _write(tmp_path, symbols, written_at):
        import json
        (tmp_path / 'ohlcv').mkdir(exist_ok=True)
        p = tmp_path / '_tracked_symbols.json'
        p.write_text(json.dumps({'symbols': symbols, 'written_at': written_at}))
        return str(tmp_path)

    def test_a_fresh_manifest_loads(self, tmp_path):
        from scripts.cache_integrity_check import load_tracked_symbols

        d = self._write(tmp_path, ['BTC', 'TON'], TODAY.isoformat())
        assert load_tracked_symbols(data_dir=d, today=TODAY) == {'BTC', 'TON'}

    def test_a_stale_manifest_is_refused(self, tmp_path):
        from scripts.cache_integrity_check import (
            TRACKED_MANIFEST_MAX_AGE_DAYS,
            load_tracked_symbols,
        )

        old = (TODAY - datetime.timedelta(days=TRACKED_MANIFEST_MAX_AGE_DAYS + 1))
        d = self._write(tmp_path, ['BTC'], old.isoformat())
        assert load_tracked_symbols(data_dir=d, today=TODAY) is None

    def test_a_missing_manifest_is_refused(self, tmp_path):
        from scripts.cache_integrity_check import load_tracked_symbols

        assert load_tracked_symbols(data_dir=str(tmp_path), today=TODAY) is None

    def test_a_corrupt_manifest_is_refused(self, tmp_path):
        from scripts.cache_integrity_check import load_tracked_symbols

        (tmp_path / '_tracked_symbols.json').write_text('{not json')
        assert load_tracked_symbols(data_dir=str(tmp_path), today=TODAY) is None

    def test_an_empty_symbol_list_is_refused(self, tmp_path):
        """An empty universe would mark EVERY symbol untracked and silence
        Check F completely — refuse rather than believe it."""
        from scripts.cache_integrity_check import load_tracked_symbols

        d = self._write(tmp_path, [], TODAY.isoformat())
        assert load_tracked_symbols(data_dir=d, today=TODAY) is None


# ---------------------------------------------------------------------------
# Checks E/F read the instrument master, and degrade LOUDLY
# ---------------------------------------------------------------------------

class TestInstrumentMasterWiring:
    """The store must never silently become the reason a check sees nothing.

    `instrument_master.load_store` degrades to an EMPTY store on a missing or
    corrupt file, and `frozen_series` skips any symbol not in `tracked`. So an
    empty store handed straight to Check F would make it examine NOTHING and
    still report success — the exact blindness the instrument master exists to remove.
    On prod, scan() produced byte-identical findings (26) with the store and
    with the hardcoded fallback.
    """

    def _mod(self):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            'cic_ac3', 'scripts/cache_integrity_check.py')
        mod = importlib.util.module_from_spec(spec)
        sys.modules['cic_ac3'] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_untrustworthy_store_returns_none_not_an_empty_set(self, tmp_path):
        """None means 'cannot judge' and triggers the announced fallback; an
        empty dict would mean 'nothing is tracked' and blind Check F."""
        cic = self._mod()
        assert cic.load_instrument_records(data_dir=str(tmp_path)) is None
        (tmp_path / '_instrument_master.json').write_text('not json')
        assert cic.load_instrument_records(data_dir=str(tmp_path)) is None

    def test_stale_store_is_refused(self, tmp_path):
        import datetime
        import json
        old = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        (tmp_path / '_instrument_master.json').write_text(json.dumps({
            'instruments': {'BTC': {'id': 'BTC', 'ticker': 'BTC'}},
            'written_at': old + 'T00:00:00'}))
        cic = self._mod()
        assert cic.load_instrument_records(data_dir=str(tmp_path)) is None

    def test_derived_and_tracked_read_the_right_fields(self):
        import sys
        sys.path.insert(0, '.')
        import instrument_master as im
        cic = self._mod()
        recs = {
            'BTC_D': im.InstrumentRecord(id='BTC_D', ticker='BTC.D',
                                         cache_stem='BTC_D',
                                         authoritative_source='derived',
                                         tracking_state='tracked'),
            'BTC': im.InstrumentRecord(id='BTC', ticker='BTC',
                                        cache_stem='BTC',
                                        authoritative_source='binance',
                                        tracking_state='tracked'),
            'GRAM': im.InstrumentRecord(id='GRAM', ticker='GRAM',
                                         cache_stem='GRAM',
                                         authoritative_source='unknown',
                                         tracking_state='orphaned'),
        }
        assert cic.derived_from_records(recs) == {'BTC_D'}
        assert cic.tracked_from_records(recs) == {'BTC_D', 'BTC'}

    def test_checks_accept_an_injected_derived_set(self):
        """Both Check E and Check F must take the set as a parameter — a module
        global cannot be swapped per-run and is how the two drifted apart."""
        import inspect
        cic = self._mod()
        assert 'derived' in inspect.signature(cic.frozen_derived_series).parameters
        assert 'derived' in inspect.signature(cic.frozen_series).parameters


class TestWrittenVsGained:
    """GT/LEO/PI/BGB were rewritten every cycle and gained no bars,
    while LBTC was not written at all. Both are STALE; they are different
    faults with different owners, and the finding must say which."""

    def test_rewritten_today_but_no_new_bar_is_named_as_a_fetch_fault(self):
        f = frozen_series({'GT': _daily(300, end_offset=500)}, today=TODAY,
                          written={'GT': TODAY})
        assert len(f) == 1
        assert 'rewritten' in f[0] and 'gains nothing' in f[0]

    def test_not_written_for_weeks_is_named_as_a_collector_fault(self):
        f = frozen_series({'LBTC': _daily(300, end_offset=16)}, today=TODAY,
                          written={'LBTC': TODAY - datetime.timedelta(days=16)})
        assert len(f) == 1
        assert 'not been written' in f[0] and '16d' in f[0]

    def test_the_false_claim_is_gone(self):
        f = frozen_series({'TON': _daily(300, end_offset=39)}, today=TODAY)
        assert 'nothing was raising it' not in f[0]
