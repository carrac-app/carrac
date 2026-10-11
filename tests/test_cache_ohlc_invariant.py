"""Check H: a bar whose high is below its own open/close (or low
above them) is not a price — it is two sources stitched into one row.

Observed: 1,325 material violations across 12 symbols, 977 of
XMR's 983 carrying open == previous close while high/low/close came from a
different session boundary. That mass is historical backfill corruption and
belongs to a repair job, not a daily alarm — so this check only judges
RECENT bars, where a violation means the corruption is happening NOW.

Tolerance is 0.1% of close: FX feeds routinely leave the high a rounding
step below the open (421 such bars across 22 symbols) and flagging those
would train everyone to ignore the alert.
"""
import datetime

from scripts.cache_integrity_check import ohlc_bar_violations

TODAY = datetime.date(2026, 8, 24)


def _bar(day, o, h, lo, c):
    return (datetime.date.fromisoformat(day), o, h, lo, c)


class TestTheInvariant:
    def test_high_below_open_is_flagged(self):
        f = ohlc_bar_violations('XMR', [_bar('2026-08-20', 54.23, 34.3, 30.15, 32.75)],
                                today=TODAY)
        assert len(f) == 1
        assert f[0].startswith('XMR: BAD-BAR-2026-08-20')
        assert 'high' in f[0]

    def test_the_dedup_token_carries_the_date(self):
        """main()'s dedup key is `SYMBOL:<first token>`, so the
        date must live INSIDE the first token (the BOARD:CARRIED-<date>
        precedent). With a dateless `BAD-BAR` token, a second corrupt date on
        the same symbol within the 30-day window would be deduplicated into
        the first and never announced."""
        f = ohlc_bar_violations('XMR', [_bar('2026-08-20', 54.23, 34.3, 30.15, 32.75)],
                                today=TODAY)
        token = f[0].split(': ', 1)[1].split(' ', 1)[0]
        assert token == 'BAD-BAR-2026-08-20'

    def test_low_above_close_is_flagged(self):
        f = ohlc_bar_violations('EURHUF',
                                [_bar('2026-08-20', 354.79, 361.21, 354.46, 353.10)],
                                today=TODAY)
        assert len(f) == 1
        assert 'low' in f[0]

    def test_a_valid_bar_is_silent(self):
        assert ohlc_bar_violations('BTC', [_bar('2026-08-20', 100.0, 110.0, 95.0, 105.0)],
                                   today=TODAY) == []


class TestTheNoiseFloor:
    def test_rounding_step_violations_stay_silent(self):
        # AUDUSD 2024-07-15 shape: high one rounding step below the open.
        f = ohlc_bar_violations('AUDUSD',
                                [_bar('2026-08-20', 0.67633009, 0.67626059, 0.67153978, 0.67633009)],
                                today=TODAY)
        assert f == []

    def test_zero_or_negative_legs_are_not_judged(self):
        assert ohlc_bar_violations('X', [_bar('2026-08-20', 0.0, 0.0, 0.0, 0.0)],
                                   today=TODAY) == []


class TestTheWindow:
    def test_historical_corruption_does_not_alarm_daily(self):
        # XMR's 983 pre-2024 bad bars must NOT become a standing daily finding.
        f = ohlc_bar_violations('XMR', [_bar('2021-05-19', 342.02, 294.21, 181.69, 200.88)],
                                today=TODAY)
        assert f == []

    def test_the_finding_is_date_stamped_so_recurrence_survives_dedup(self):
        a = ohlc_bar_violations('X', [_bar('2026-08-19', 10.0, 8.0, 7.0, 7.5)], today=TODAY)
        b = ohlc_bar_violations('X', [_bar('2026-08-20', 10.0, 8.0, 7.0, 7.5)], today=TODAY)
        assert a[0].split(' — ')[0] != b[0].split(' — ')[0]
