"""The host integrity monitor must see a ONE-session hole.

Its Check A uses GAP_DAYS=45, sized for a multi-year hole (1129 days). A
one-session hole slips under that: with a single session missing, the free
public /brief could publish "Gold surged +5.6% today" off a pair spanning two
sessions while Check A logs "healthy" — correctly, by its own rules.

Check C closes that. The interesting part is HOW it avoids becoming noise:
a single symbol cannot tell a real hole from a public holiday (both are a
missing weekday, and recognising holidays needs an exchange calendar the
script deliberately does not carry). The board can — on a holiday nobody
has the day, on a fetch failure most symbols do. So the cohort votes.

The script is stdlib-only and runs on the host, so these exercise the pure
function directly.
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


TODAY = datetime.date(2026, 8, 6)


def _sessions(*iso):
    return [datetime.date.fromisoformat(s) for s in iso]


# Real trading days around a one-session hole. 08-01/02 was a weekend.
FULL = _sessions("2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30",
                 "2026-07-31", "2026-08-03", "2026-08-04", "2026-08-05",
                 "2026-08-06")
GAPPED = [d for d in FULL if d != datetime.date(2026, 8, 5)]


def _board(healthy=40, gapped=()):
    """A board of `healthy` complete symbols plus named gapped ones."""
    data = {f"OK{i}": list(FULL) for i in range(healthy)}
    for sym in gapped:
        data[sym] = list(GAPPED)
    return data


def test_the_real_gold_hole_is_reported():
    out = cic.recent_session_gaps(_board(gapped=["GOLD"]), today=TODAY)
    assert len(out) == 1
    assert out[0].startswith("GOLD: MISSING SESSION 2026-08-05")
    assert "peers have it" in out[0]


def test_all_nine_are_reported():
    nine = ["COPPER", "EURHUF", "EURUSD", "GOLD", "NATGAS",
            "OIL", "PLATINUM", "SILVER", "USDCHF"]
    out = cic.recent_session_gaps(_board(gapped=nine), today=TODAY)
    assert len(out) == 9
    assert {o.split(":")[0] for o in out} == set(nine)


def test_a_healthy_board_is_silent():
    assert cic.recent_session_gaps(_board(), today=TODAY) == []


def test_a_public_holiday_is_NOT_reported():
    """The whole point of the cohort vote.

    When EVERY weekday-only symbol is missing the same day, that day was not
    a session — it was a holiday. A per-symbol day-count would flag all 40;
    the vote flags none.
    """
    board = {f"OK{i}": list(GAPPED) for i in range(40)}   # nobody has 08-05
    assert cic.recent_session_gaps(board, today=TODAY) == []


def test_crypto_does_not_get_a_vote():
    """Crypto trades weekends. Left in the tally it would make every weekend
    day look like a session and every equity symbol look permanently gappy."""
    board = _board(healthy=25, gapped=["GOLD"])
    weekend = _sessions("2026-08-01", "2026-08-02")
    for i in range(300):                       # crypto swamps by count
        board[f"COIN{i}"] = sorted(FULL + weekend)
    out = cic.recent_session_gaps(board, today=TODAY)
    assert [o.split(":")[0] for o in out] == ["GOLD"]


def test_a_stale_symbol_is_not_flagged_for_every_later_session():
    """A tail from June is a staleness question (Check A / the app's own
    freshness check), not an interior hole. It must not emit one finding per
    missing session."""
    board = _board(gapped=[])
    board["BDX"] = _sessions("2026-07-27", "2026-07-28", "2026-07-29")
    assert cic.recent_session_gaps(board, today=TODAY) == []


def test_known_legitimate_gaps_are_skipped():
    sym = sorted(cic.KNOWN_LEGITIMATE_GAPS)[0]
    out = cic.recent_session_gaps(_board(gapped=[sym]), today=TODAY)
    assert out == []


def test_too_few_voters_says_nothing_rather_than_guessing():
    """A near-empty DATA_DIR must not manufacture findings."""
    board = {f"OK{i}": list(FULL) for i in range(3)}
    board["GOLD"] = list(GAPPED)
    assert cic.recent_session_gaps(board, today=TODAY) == []


def test_older_holes_are_outside_the_window():
    """Check C is about recent sessions; ancient history is Check A's job."""
    old = datetime.date(2026, 1, 5)
    board = _board(gapped=[])
    board["GOLD"] = sorted([old] + FULL)
    assert cic.recent_session_gaps(board, today=TODAY) == []


def test_multiple_missing_sessions_are_summarised():
    board = _board(gapped=[])
    board["GOLD"] = [d for d in FULL
                     if d not in _sessions("2026-07-29", "2026-08-04", "2026-08-05")]
    out = cic.recent_session_gaps(board, today=TODAY)
    assert len(out) == 1
    assert "2026-07-29" in out[0] and "2026-08-05" in out[0]


def test_scan_wires_check_c_in():
    """Built-but-not-connected is this codebase's most common defect."""
    import inspect
    assert "recent_session_gaps(symbol_dates)" in inspect.getsource(cic.scan)


def test_foreign_calendar_indices_are_not_reported():
    """The cohort vote is US-dominated, so a real Tokyo/HK/Seoul holiday reads
    as a hole. On a real board the only three findings were NIKKEI/HSI/KOSPI
    and all three were legitimate local closures — NIKKEI's 2026-07-20 is
    Marine Day, the third Monday of July."""
    out = cic.recent_session_gaps(_board(gapped=["NIKKEI", "HSI", "KOSPI"]), today=TODAY)
    assert out == []


def test_foreign_indices_still_get_a_vote():
    """Exempt from being REPORTED, not from voting — their sessions are real."""
    board = {("NIKKEI" if i == 0 else f"OK{i}"): list(FULL) for i in range(25)}
    board["GOLD"] = list(GAPPED)
    out = cic.recent_session_gaps(board, today=TODAY)
    assert len(out) == 1 and out[0].startswith("GOLD:")
    assert "/26 peers" in out[0]          # NIKKEI counted toward the quorum


def test_a_symbol_with_weekend_bars_is_still_CHECKED():
    """Voting and checking are separate populations.

    On the real board GOLD, EURUSD, EURHUF and
    USDCHF all carry occasional Sunday bars, so gating reporting on cohort
    membership would make them silently exempt — including GOLD, the symbol
    the one-session-hole scenario is built around.
    """
    board = _board(healthy=40, gapped=[])
    sunday = _sessions("2026-07-19", "2026-07-26", "2026-08-02")
    board["GOLD"] = sorted(GAPPED + sunday)      # weekend bars AND the hole
    out = cic.recent_session_gaps(board, today=TODAY)
    assert len(out) == 1, out
    assert out[0].startswith("GOLD: MISSING SESSION 2026-08-05")


def test_weekend_carrying_symbols_still_do_not_vote():
    """They must not define the calendar — only be measured against it."""
    board = {f"OK{i}": list(FULL) for i in range(25)}
    weekend = _sessions("2026-08-01", "2026-08-02")
    for i in range(300):
        board[f"COIN{i}"] = sorted(FULL + weekend)
    out = cic.recent_session_gaps(board, today=TODAY)
    assert out == [], out          # nobody is missing a weekday session


def test_the_three_fx_pairs_missing_aug05_are_reported():
    """Three FX pairs lacking one session (2026-08-05 on this board) are reported."""
    board = _board(healthy=40, gapped=[])
    sunday = _sessions("2026-07-19", "2026-07-26", "2026-08-02")
    for s in ("EURUSD", "EURHUF", "USDCHF"):
        board[s] = sorted(GAPPED + sunday)
    out = cic.recent_session_gaps(board, today=TODAY)
    assert {o.split(":")[0] for o in out} == {"EURUSD", "EURHUF", "USDCHF"}
