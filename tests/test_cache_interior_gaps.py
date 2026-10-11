"""Check A gains weekday awareness.

GAP_DAYS=45 was sized for a 1129-day BTC hole and
counts CALENDAR days, so a weekday-only instrument can lose six weeks of
sessions (~31 weekdays inside 44 calendar days) and stay invisible to it,
while Check C only sees ONE-session holes inside its 21-day cohort window.
The wedge between them is closed by counting missing WEEKDAY sessions: no
US-calendar market closure since 2001 has exceeded 4 consecutive sessions,
so an interior hole of more than GAP_WEEKDAYS=5 missing weekdays is a fetch
fault, not a holiday. Foreign-calendar symbols (Golden Week, LNY) keep the
calendar-only rule, same as their Check C exemption.

Measured on the full prod board before setting the threshold:
ZERO symbols carry an interior hole of >5 missing weekdays under 45 calendar
days — the sub-check arrives green, so any future finding is real signal
(the no-alarm-nobody-reads rule).

The script is stdlib-only and runs on the host; tests exercise the pure
function directly, same as test_cache_integrity_sessions.py.
"""
import datetime
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "cache_integrity_check",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cache_integrity_check.py",
)
cic = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cic)


def _weekdays(start, n, skip=()):
    """n consecutive weekday dates from start, omitting any in skip."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def _daily(start, n):
    """n consecutive calendar dates (a 24/7 crypto series)."""
    return [start + datetime.timedelta(days=i) for i in range(n)]


def test_calendar_rule_unchanged_for_crypto():
    start = datetime.date(2026, 1, 1)
    dates = _daily(start, 30) + _daily(start + datetime.timedelta(days=80), 30)
    finding = cic.interior_gap_finding("BTC", sorted(dates))
    assert finding is not None and "GAP" in finding


def test_weekday_instrument_multiweek_hole_is_caught():
    """Eight missing weekday sessions inside ~12 calendar days: invisible to
    the 45-day calendar rule, exactly the class this closes."""
    start = datetime.date(2026, 1, 5)  # a Monday
    full = _weekdays(start, 80)
    hole = set(full[25:33])  # 8 consecutive sessions
    dates = [d for d in full if d not in hole]
    finding = cic.interior_gap_finding("COPPER", dates)
    assert finding is not None, "8 missing weekday sessions went unflagged"
    assert "weekday" in finding


def test_four_session_closure_is_a_holiday_not_a_finding():
    """The worst real US closure (2001) was 4 sessions."""
    start = datetime.date(2026, 1, 5)
    full = _weekdays(start, 80)
    hole = set(full[25:29])  # 4 sessions
    dates = [d for d in full if d not in hole]
    assert cic.interior_gap_finding("COPPER", dates) is None


def test_foreign_calendar_symbol_keeps_calendar_rule_only():
    start = datetime.date(2026, 1, 5)
    full = _weekdays(start, 80)
    hole = set(full[25:33])  # 8 sessions — Golden Week territory
    dates = [d for d in full if d not in hole]
    assert "NIKKEI" in cic.FOREIGN_SESSION_CALENDARS
    assert cic.interior_gap_finding("NIKKEI", dates) is None


def test_crypto_short_hole_not_judged_by_weekday_rule():
    """A 24/7 series is not a weekday instrument; sub-45d holes stay Check
    C/G territory for it."""
    start = datetime.date(2026, 1, 1)
    dates = _daily(start, 30) + _daily(start + datetime.timedelta(days=50), 30)
    assert cic.interior_gap_finding("BTC", sorted(dates)) is None


def test_known_legitimate_gap_symbol_is_exempt():
    start = datetime.date(2026, 1, 5)
    full = _weekdays(start, 80)
    hole = set(full[25:40])
    dates = [d for d in full if d not in hole]
    assert "EOS" in cic.KNOWN_LEGITIMATE_GAPS
    assert cic.interior_gap_finding("EOS", dates) is None
