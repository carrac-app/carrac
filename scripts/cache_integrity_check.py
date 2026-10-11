#!/usr/bin/env python3
"""OHLCV cache integrity monitor — catches silent corruption of data/*.csv.

Runs on the HOST (stdlib only, no container, works even if the app is down).

Cron (host crontab): 20 6 * * * <repo>/scripts/cache_integrity_check.py \
    >> /var/log/carrac-cache-integrity.log 2>&1

Paths: CARRAC_DATA_DIR (default: ./data in the checkout), plus two small state
files kept under <data dir>/.cache-integrity/ (CARRAC_CACHE_BASELINE,
CARRAC_CACHE_STATE override them).

Why this exists
---------------
Once, BTC_daily.csv silently went 3259 rows -> 1503 -> 376 with a
1129-day interior hole, and the BTC chart rendered a fake vertical 28k->65k
spike. Nothing caught it for days: pandas parses a truncated CSV without
raising, the app's freshness check only reads the LAST row (so a fresh tail
hides any interior hole forever), the smoke suite checks status codes and
latency, gold-check checks cache hit RATES not cache CORRECTNESS, and no
exception was ever thrown so the error tracker stayed quiet.

Five independent checks, because each observed failure looks different and no
one of them can see the others:

  A. interior gaps   — the 376-row state (a hole between two blocks)
  B. row shrinkage   — the 1503-row state, which had NO gap at all; only the
                       row count against a high-water mark reveals it
  C. one-session holes — a single missing day, too small for A
  D. phantom weekend bars — a SURPLUS bar, which A/B/C cannot represent
  E. frozen derived series — a file that is internally perfect and simply
                       stopped advancing; A/C look between rows, B counts
                       rows, D looks for surplus, so a stale TAIL passes all
                       four
  F. any frozen symbol — same blindness, generalised to all 350+ caches with
                       a threshold each series calibrates from its own
                       cadence
  G. carried-forward backfill bars — a session that is PRESENT, non-empty,
                       and partly copied from its predecessor. A/C ask "is a
                       bar missing", D asks "is there a surplus bar", the
                       synthetic mask asks "is it empty" — a late backfill
                       that copies the previous session's volume satisfies
                       all of them

Alerts are state-based (only on transitions), matching quant-health-check.sh.
"""
import csv
import datetime
import json
import os
import sys
import urllib.request

# Base URL of your ntfy server. Set it to https://ntfy.sh, or to a self-hosted
# instance if your host cannot reach ntfy.sh. No default: the address depends
# on where ntfy runs for your deployment, so it comes from the environment.
# Supplied by an `NTFY_BASE=` line at the top of the crontab (or, for
# container-run scripts, by the container env). Empty means alerting is off,
# and the send path says so.
NTFY_BASE = os.environ.get("NTFY_BASE", "")

DATA_DIR = os.environ.get(
    "CARRAC_DATA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
_STATE_DIR = os.path.join(DATA_DIR, ".cache-integrity")
BASELINE_FILE = os.environ.get(
    "CARRAC_CACHE_BASELINE", os.path.join(_STATE_DIR, "rowcounts.json"))
STATE_FILE = os.environ.get(
    "CARRAC_CACHE_STATE", os.path.join(_STATE_DIR, "state.json"))
# No default. Supplied by an `NTFY_CARRAC_TOPIC=` line in the crontab header.
NTFY_CARRAC_TOPIC = os.environ.get("NTFY_CARRAC_TOPIC", "")

GAP_DAYS = 45          # a hole bigger than this is not a weekend/holiday

# Check A's weekday extension. GAP_DAYS counts CALENDAR days —
# sized for the BTC truncation case, a 1129-day hole — so a weekday-only
# instrument can lose ~31 sessions inside 44 calendar days and stay invisible
# to it, while Check C only sees ONE-session holes inside its 21-day cohort
# window. This closes the wedge: an interior hole missing more than
# GAP_WEEKDAYS weekday sessions is a fetch fault, not a holiday — no
# US-calendar closure since 2001 (4 sessions) has come close. Foreign-calendar
# symbols (Golden Week, LNY can run longer) keep the calendar-only rule, same
# as their Check C reporting exemption. Across the full board the
# threshold yields ZERO findings, so the
# sub-check arrives green and any future finding is real signal.
GAP_WEEKDAYS = 5

# Check C — one-session holes. GAP_DAYS above cannot see these.
SESSION_WINDOW_DAYS = 21   # lookback for the cohort vote
MIN_SESSION_VOTERS = 20    # below this the vote is not meaningful
SESSION_QUORUM = 0.60      # share of the 5-day-week cohort that makes a session

# Symbols on a NON-US exchange calendar. The cohort vote is dominated by US
# names, so a genuine local holiday reads as a hole. On the real board,
# Check C's only three findings were all of this kind
# and all legitimate — e.g. NIKKEI missing a day that is Marine Day
# (third Monday of July), a real Tokyo market closure.
#
# They still get a VOTE (they are 5-day-week names and their sessions are
# real); they are only exempt from being REPORTED, because a US-majority
# quorum cannot speak for Tokyo, Hong Kong or Seoul. Adding a proper exchange
# calendar would be the real fix; this script is stdlib-only by design so that
# it runs when the app is down.
FOREIGN_SESSION_CALENDARS = {
    "NIKKEI", "HSI", "HSTECH", "KOSPI", "DAX", "FTSE", "CAC", "BUX",
}
SHRINK_ALERT = 0.80    # alert if a file drops below this share of high-water
MIN_ROWS_TRACKED = 30  # ignore files still bootstrapping

# Curated one-shot corrections to the row-count high-water mark.
# The mark is deliberately sticky (see --reset-baseline), so a correction that
# legitimately REMOVES bogus rows — predecessor history welded in before the
# token's real inception — reads as a collapse and pages every morning.
# Recording the reset here instead of running an ad-hoc --reset-baseline on
# the host keeps the author, the date and the reason in git, the same shape
# instrument_master.LIFECYCLE_OVERRIDES uses for delistings. Keyed to the
# exact inflated mark: applied only while the stored mark still equals it, so
# an entry can never clamp a mark the series has legitimately regrown past.
BASELINE_CORRECTIONS = {
    'PI': {
        'inflated_mark': 714, 'corrected_mark': 552,
        'author': 'owner', 'effective_date': '2026-08-25',
        'note': 'Pre-inception rows removed; the series correctly starts at '
                'the 2025-02-20 token inception (552 rows / 552-day span).',
    },
    'SKY': {
        'inflated_mark': 659, 'corrected_mark': 343,
        'author': 'owner', 'effective_date': '2026-08-25',
        'note': 'MKR predecessor history removed; SKY correctly starts at '
                'its 2025-09-17 inception (343 rows / 343-day span).',
    },
}

# Pairs whose gaps are REAL market history (delisted / rebranded), verified
# against the upstream sources: the source returns rows on both
# sides of the window and none inside, or nothing at all. Do not "repair"
# these — there are no bars to fetch. Remove a symbol here if it relists.
KNOWN_LEGITIMATE_GAPS = {
    "ARC", "BEAM", "EOS", "EPX", "FTM", "FTT", "KCS", "LIT", "MKR",
    "OCEAN", "WBT",
}


# Check D — weekend bars on a weekday-only instrument.
# Detection is by the series' OWN shape, not a symbol list, so it covers
# dynamic symbols this script knows nothing about. A 24/7 instrument sits near
# 2/7 == 0.286; the phantom cases run 0.0009 to 0.0218.
# The gap between those populations is two orders of magnitude, so the exact
# threshold is not load-bearing.
WEEKEND_TRADER_RATIO = 0.10
MIN_ROWS_FOR_WEEKEND_VERDICT = 60


# Check E — a derived series that stopped advancing.
#
# TOTAL/TOTAL2/TOTAL3/BTC.D/USDT.D/OTHERS.D and the *BTC ratios are recomputed
# over a rolling window from top-coin market caps. When the upstream fetch
# fails, database.py::_get_crypto_metric_ohlcv serves the last-good CSV and
# logs nothing louder than a warning — so the chart keeps rendering and simply
# stops moving. No error, no banner, no staleness marker.
#
# Checks A–D are all blind to this by construction: A and C look for holes
# BETWEEN rows, B compares row COUNTS (a frozen file keeps its count), and D
# looks for surplus weekend bars. A file whose tail is three weeks old and
# otherwise perfect passes every one of them.
#
# Scoped deliberately to the DERIVED series rather than every cache: these are
# 24/7 crypto aggregates with a same-day cadence, so the threshold is
# decidable. Ordinary symbols have exchange calendars, listing gaps and
# holidays — those are Check F's job, via a self-calibrating cadence.
#
# THRESHOLD: 3 days. The derived series legitimately lags its constituents by
# up to a day, so 1 would be noise; all eleven
# tails are normally same-day. Three days is comfortably outside normal operation and
# still catches a freeze in days rather than the 46 it once took.
#
# Names are the ON-DISK forms — the '.' in BTC.D becomes '_' in the filename.
# Must mirror database.py::crypto_metric_map; this script is stdlib-only by
# design (it has to run when the app is down) so it cannot import that map.
DERIVED_SERIES = {
    "TOTAL", "TOTAL2", "TOTAL3", "BTC_D", "USDT_D", "OTHERS_D",
    "ETHBTC", "XRPBTC", "AVAXBTC", "TOTAL3BTC", "ALTBTC",
}
DERIVED_STALE_DAYS = 3


def frozen_derived_series(symbol_dates, today=None, derived=None):
    """Flag derived market-cap series whose newest bar has stopped advancing.

    ``derived`` comes from the instrument master; it falls back
    to the hardcoded DERIVED_SERIES only when the store cannot be trusted, and
    the caller announces that.
    """
    today = today or datetime.date.today()
    derived = DERIVED_SERIES if derived is None else derived
    findings = []
    for symbol in sorted(derived & set(symbol_dates)):
        dates = symbol_dates[symbol]
        if not dates:
            continue
        age = (today - max(dates)).days
        if age > DERIVED_STALE_DAYS:
            findings.append(
                f"{symbol}: FROZEN — newest bar is {max(dates)} ({age}d old, "
                f"limit {DERIVED_STALE_DAYS}d). The market-cap source is "
                f"failing and the chart is still rendering the last-good "
                f"window, so it looks fine and has simply stopped moving."
            )
    return findings


# Check F — ANY symbol that quietly stopped collecting.
#
# A chart can sit frozen for weeks with nothing telling anyone.
# The app KNEW — `_is_history_stale` computes it per asset and the UI publishes
# an honest "no data since X, so no readings" block — but the only ALARM,
# carrac/precompute.py::_report_stale_assets, fires solely when
# `len(newly) >= _STALE_ALARM_JUMP` (default 3) in a SINGLE cycle, and
# `prev_count is None` suppresses the first cycle after every restart. One
# symbol dying alone never alarms. Checks A–E cannot see it either: A and C
# look BETWEEN rows, B counts rows, D looks for surplus weekend bars, and E is
# scoped to the eleven derived series.
#
# THRESHOLD, self-calibrating. Each series sets its own bar from its own
# median inter-bar gap, so there is no asset-class map to keep in sync and no
# exchange calendar to encode — the same "judge by the series' own shape"
# approach Check D uses for weekend trading. A 24/7 crypto series calibrates to
# ~1.0d, an equity to ~1.4d (weekends absorbed), a weekly series to ~7d, and
# each is judged against its own normal.
#
# TWO FLOORS, because the median gap cannot see a market HOLIDAY. Equities,
# FX and commodities all calibrate to cadence 1.0 (weekend gaps are a minority
# of gaps, so the median is 1), which would put their limit at 4 days — and a
# Golden Week, Lunar New Year or Christmas closure runs longer than that. On
# the day that alarm fired it would flag every equity at once, which is how a
# guard becomes noise and then gets ignored.
#
# So the floor is chosen from the series' own weekend behaviour, reusing Check
# D's measured primitive: a series that trades weekends has no market closures
# to absorb and gets the tight floor; a weekday-only series gets one wide
# enough for the longest routine exchange holiday.
STALE_CADENCE_WINDOW = 90     # bars of recent history that measure cadence
STALE_CADENCE_MULTIPLE = 4.0  # tail may be this many normal gaps old
STALE_FLOOR_247_DAYS = 4      # 24/7 series (crypto): nothing legitimately pauses
STALE_FLOOR_CALENDAR_DAYS = 10  # weekday-only: clears Golden Week / LNY / Christmas
# Bounds the one real weakness of self-calibration: a series that dies SLOWLY
# widens its own median gap and could raise its own bar out of reach. Past this
# cadence we stop trusting the measurement and fall back to a flat ceiling.
STALE_MAX_CADENCE_DAYS = 10.0
STALE_CEILING_DAYS = 45       # nothing is allowed to be older than this, ever
MIN_ROWS_FOR_STALE_VERDICT = 30


def is_synthetic_row(open_, high, low, close, volume):
    """True if this row records no trading — OHLC identical AND zero volume.

    The stdlib twin of ``carrac.staleness.synthetic_bar_mask``, which is the
    app's single definition of the same thing and is shared by the read guard,
    the write guard and the purge script. This script cannot import it: it is
    stdlib-only on purpose so it still runs when the app is down, and the host
    has no pandas outside the container.

    So the rule is written twice, which is the exact setup that caused
    "what counts as a real bar" decided in one place and assumed
    in another. ``TestTheSyntheticRuleDoesNotDriftFromTheApp`` runs both over
    the same rows and fails if they ever diverge. Change one, change both.

    Both halves are required, and each rules out a different real thing: a flat
    bar with real volume is a genuine no-range session; a zero-volume bar that
    moved is a venue under-reporting turnover.

    CAUTION: this alone does not mean "fabricated". A fabricated bar is only
    recognisable RELATIVE to real ones — see the fallback in ``scan``.
    """
    try:
        o, h, lo, c = float(open_), float(high), float(low), float(close)
    except (TypeError, ValueError):
        # pandas coerces junk to NaN and NaN == NaN is False, so an unreadable
        # bar is unreadable, not fabricated. Calling it fabricated would let a
        # parse failure purge real history.
        return False
    try:
        vol = float(volume)
    except (TypeError, ValueError):
        vol = 0.0  # matches `vol.fillna(0)` on the app side
    return vol == 0 and o == h == lo == c


def _median(values):
    s = sorted(values)
    n = len(s)
    if not n:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


TRACKED_MANIFEST = "_tracked_symbols.json"
TRACKED_MANIFEST_MAX_AGE_DAYS = 3


def load_instrument_records(data_dir=None, today=None):
    """The instrument-master records, or None if we cannot trust them.

    Written every full precompute cycle by
    ``carrac/precompute.py::_write_instrument_master``, and read here so Check E
    and Check F stop relying on a hand-maintained list of symbol names.

    Same "cannot judge" contract as ``load_tracked_symbols`` below, for the same
    reason: ``instrument_master.load_store`` degrades to an EMPTY store on a
    missing or corrupt file. Returning that empty dict would silently tell
    Check F that nothing is tracked, and its filter skips anything untracked —
    so every symbol would be skipped and the check would pass while examining
    NOTHING. That is the exact failure this whole check exists to stop, so a
    store we cannot trust must return None and be announced, never used.

    Age threshold is deliberately the SAME as the manifest's: both files are
    written by the same precompute cycle, so a different number here would be
    arbitrary. If the store is older than the manifest, the store writer broke
    specifically — and that is what the age check catches.

    stdlib only. ``instrument_master`` lives at the repo root, one level above
    this script, and is stdlib-only at module scope precisely so this cron can
    import it without pulling in ``carrac/__init__``'s Sentry/PostHog bootstrap.
    sys.path[0] here is ``scripts/``, not the repo root, so the parent has to be
    added explicitly — without this the import fails and Check F silently loses
    the store.
    """
    today = today or datetime.date.today()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        import instrument_master
        store = instrument_master.load_store(data_dir=data_dir or DATA_DIR)
        written_at = store.get("written_at")
        records = instrument_master.load_records(data_dir=data_dir or DATA_DIR)
        if not records or not written_at:
            return None
        age = (today - datetime.date.fromisoformat(written_at[:10])).days
        if age > TRACKED_MANIFEST_MAX_AGE_DAYS:
            return None
        return records
    except Exception:
        return None


def derived_from_records(records):
    """The derived (computed, not traded) series, per the instrument master.

    Replaces judging by a hardcoded name list. ``authoritative_source == 'derived'`` reproduces
    DERIVED_SERIES exactly — all 11, nothing added, nothing dropped — which is
    the only basis on which swapping a guard's input is safe.
    """
    return {rid for rid, rec in records.items()
            if getattr(rec, "authoritative_source", None) == "derived"}


def tracked_from_records(records):
    """The symbols the app tracks AND has a cache file for."""
    return {rid for rid, rec in records.items()
            if getattr(rec, "tracking_state", None) == "tracked"}


def delisted_from_records(records):
    """The instruments a human has recorded as delisted.

    Check F must not keep paging about these. A curated delisting is a decision
    already taken, with an author and a date in
    ``instrument_master.LIFECYCLE_OVERRIDES`` — re-reporting it every morning
    as "collection stopped" is the ignored-alarm pattern, and it is the same
    reasoning this check already applies to a symbol the app no longer tracks:
    nobody can fix it, so it is not an alert.

    Read from the STORE rather than importing the constant, deliberately. The
    store is what the app actually acted on; if the two ever disagree, the
    check should reflect what shipped, not what the source says should have.
    Degrades to an empty set when the store is missing, which fails LOUD — the
    findings come back rather than being silently suppressed.
    """
    return {rid for rid, rec in records.items()
            if getattr(rec, "status", None) == "delisted"}


def rows_collapsed_findings(counts, baseline, delisted=None):
    """Check B: row-count drop vs the sticky high-water mark.

    ``baseline`` is mutated in place (marks ratchet up), matching what main()
    persists. Two exemptions, both curated human decisions, never heuristics:

    - ``delisted``: a recorded delisting truncates a series to its true
      end-of-life, and re-reporting that trim every morning is
      the ignored-alarm pattern. Same contract as Check F: ``None`` means
      "cannot know" and the findings come back LOUD rather than being
      silently suppressed.
    - ``BASELINE_CORRECTIONS``: a one-shot re-baseline for a mark known to
      be inflated by bogus rows, applied only while the stored mark still
      equals the recorded inflated value.
    """
    findings = []
    for symbol, n in counts.items():
        prev = baseline.get(symbol, 0)
        corr = BASELINE_CORRECTIONS.get(symbol)
        if corr and prev == corr['inflated_mark']:
            prev = corr['corrected_mark']
        if (prev >= MIN_ROWS_TRACKED and n < prev * SHRINK_ALERT
                and not (delisted and symbol in delisted)):
            findings.append(
                f"{symbol}: ROWS COLLAPSED {prev} -> {n} "
                f"({n / prev:.0%} of high-water mark)"
            )
        baseline[symbol] = max(prev, n)
    return findings


def load_tracked_symbols(data_dir=None, today=None):
    """The symbols the app currently TRACKS, or None if we cannot know.

    Written each full precompute cycle by
    ``carrac/precompute.py::_write_tracked_symbols_manifest``.

    Check F needs this because a stale CSV is only worth paging about if the
    app still collects that symbol. Of 12 frozen
    caches in one sample, 9 belonged to symbols no longer tracked — leftover files. Alerting
    on those is unactionable by construction.

    Returns None (meaning "cannot judge") rather than a guess when the manifest
    is missing, unreadable, or too old to trust. A stale manifest would quietly
    re-classify live symbols as orphans, which is the more dangerous direction.
    """
    today = today or datetime.date.today()
    path = os.path.join(data_dir or DATA_DIR, TRACKED_MANIFEST)
    try:
        with open(path) as fh:
            payload = json.load(fh)
        symbols = payload.get("symbols")
        written_at = payload.get("written_at")
        if not symbols or not written_at:
            return None
        age = (today - datetime.date.fromisoformat(written_at[:10])).days
        if age > TRACKED_MANIFEST_MAX_AGE_DAYS:
            return None
        return {str(s).upper() for s in symbols}
    except Exception:
        return None



def ohlc_bar_violations(symbol, bars, today=None, window_days=30,
                        tolerance=0.001):
    """Check H: a bar whose high is below its own open/close, or
    low above them, is not a price — it is two sources stitched into one row.

    Measured: 1,325 such bars above 0.1%% of close across 12 symbols, 977 of
    XMR's 983 with open == previous close while high/low/close came from a
    different session boundary — historical backfill corruption, owned by a
    separate repair. This check judges only the last ``window_days`` so it fires
    when the stitching happens NOW, and stays quiet about the archaeology.
    Tolerance 0.1%%: FX feeds leave the high a rounding step below the open on
    hundreds of bars, and flagging those trains people to ignore the alert.
    The finding kind is date-stamped like Check D: a new bad bar is a NEW
    fault, so recurrence must survive dedup.
    """
    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(days=window_days)
    findings = []
    for d, o, h, lo, c in bars:
        if d < cutoff:
            continue
        if c <= 0 or min(o, h, lo) <= 0:
            continue
        high_short = (max(o, c) - h) / c
        low_over = (lo - min(o, c)) / c
        if high_short > tolerance:
            what = (f"high {h:g} sits {high_short:.1%} below its own "
                    f"open/close ({o:g}/{c:g})")
        elif low_over > tolerance:
            what = (f"low {lo:g} sits {low_over:.1%} above its own "
                    f"open/close ({o:g}/{c:g})")
        else:
            continue
        findings.append(
            # The date lives INSIDE the first token because
            # main()'s dedup key is `SYMBOL:<first token>` — the
            # BOARD:CARRIED-<date> precedent. A dateless token would fold a
            # second corrupt date on the same symbol into the first finding.
            f"{symbol}: BAD-BAR-{d} — {what}; one row is carrying prices "
            f"from two different sessions, and every indicator reading it "
            f"is wrong"
        )
    return findings


def frozen_series(symbol_dates, today=None, tracked=None, real_tail=None,
                  derived=None, delisted=None, written=None):
    """Flag any symbol whose newest REAL bar is far past its own normal cadence.

    ``real_tail`` maps symbol -> newest bar that records actual trading, as
    computed by ``scan``.

    Returns findings keyed ``SYMBOL: STALE ...``. Deliberately NOT date-stamped
    in the kind token, unlike Check D: a phantom weekend bar is a NEW fault
    each week, so recurrence must survive dedup, whereas a frozen series is ONE
    ongoing fault. It should alert once and clear on recovery — re-alerting
    daily as it ages is exactly the noise that trains you to ignore alerts.
    """
    today = today or datetime.date.today()
    derived = DERIVED_SERIES if derived is None else derived
    findings = []
    for symbol, dates in sorted(symbol_dates.items()):
        # Check E already owns these, with a tighter fixed rule. Two findings
        # for one fault is noise.
        if symbol in derived:
            continue
        # A leftover CSV for a symbol the app no longer collects will never
        # advance and nobody can fix it — that is a cleanup task, not an alert.
        if tracked is not None and symbol not in tracked:
            continue
        # Same rule, different reason: a curated delisting already explains why
        # this series stopped, so the finding would carry no new information.
        if delisted and symbol in delisted:
            continue
        if not dates or len(dates) < MIN_ROWS_FOR_STALE_VERDICT:
            continue

        ordered = sorted(dates)
        recent = ordered[-STALE_CADENCE_WINDOW:]
        gaps = [(recent[i] - recent[i - 1]).days for i in range(1, len(recent))]
        gaps = [g for g in gaps if g > 0]
        cadence = _median(gaps)
        if not cadence:
            continue
        cadence = min(cadence, STALE_MAX_CADENCE_DAYS)

        # Does this series trade weekends? Same measurement Check D uses.
        weekend = sum(1 for d in ordered if d.weekday() >= 5)
        trades_247 = weekend / len(ordered) > WEEKEND_TRADER_RATIO
        floor = STALE_FLOOR_247_DAYS if trades_247 else STALE_FLOOR_CALENDAR_DAYS

        tail = (real_tail or {}).get(symbol) or recent[-1]
        age = (today - tail).days
        limit = min(max(floor, cadence * STALE_CADENCE_MULTIPLE),
                    STALE_CEILING_DAYS)
        if age > limit:
            # Say when the file runs LATER than its last real bar. That gap is
            # why the series looked healthy, and a human reading the alert needs
            # it — otherwise "newest bar is 2024-07-08" invites the reply "no it
            # isn't, the chart updated this morning".
            masked = ""
            if tail < ordered[-1]:
                masked = (
                    f" The cache then runs to {ordered[-1]} on rows that record "
                    f"no trading (flat OHLC, zero volume), which is what kept "
                    f"this series looking current."
                )
            # Say whether the collector is still touching the
            # file. GT/LEO/PI/BGB were rewritten every cycle and gained
            # nothing (a fetch returning old data); LBTC was not written at
            # all (the collector never reached it). Same STALE, different
            # cause. The app's own stale-batch alarm fires on a JUMP, so a
            # standing case lands here instead.
            wrote = (written or {}).get(symbol)
            if wrote is None:
                side = ""
            elif (today - wrote).days <= 2:
                side = (
                    f" The file was rewritten {wrote}, so the collector runs "
                    f"and gains nothing — the fetch is returning old data, "
                    f"not stopping."
                )
            else:
                side = (
                    f" The file has not been written since {wrote} "
                    f"({(today - wrote).days}d) — the collector is not "
                    f"reaching this symbol."
                )
            findings.append(
                f"{symbol}: STALE — newest real bar is {tail} ({age}d old); "
                f"this series normally prints every {cadence:g}d and "
                f"{'trades weekends' if trades_247 else 'follows an exchange calendar'}, "
                f"so anything past {limit:g}d means collection stopped and no "
                f"readings are published for it. The app's stale-batch alarm "
                f"only fires on a jump, so a standing case is reported here."
                f"{side}{masked}"
            )
    return findings


def phantom_weekend_bars(symbol_dates):
    """Flag weekday-only instruments carrying weekend-dated bars.

    The two existing guards are blind to this by construction:
    ``_missing_business_days`` uses ``pd.bdate_range``, which skips weekends,
    so a SURPLUS weekend bar cannot register; and Check C below already
    notices weekend bars but only uses them to decide who may vote on what a
    session is — it never reports them.
    """
    findings = []
    for symbol, dates in sorted(symbol_dates.items()):
        if not dates or len(dates) < MIN_ROWS_FOR_WEEKEND_VERDICT:
            continue
        weekend = sorted(d for d in dates if d.weekday() >= 5)
        if not weekend:
            continue
        if len(weekend) / len(dates) > WEEKEND_TRADER_RATIO:
            continue          # trades every day (crypto) — these bars are real
        shown = ", ".join(str(d) for d in weekend[-3:])
        # The NEWEST offending date is folded into the finding's kind token so
        # main()'s alert key becomes SYMBOL:WEEKEND-<date>. Without it the key
        # is a bare SYMBOL:WEEKEND, and this writer adds a bar EVERY weekend:
        # the first alert fires, the key is recorded, and every subsequent
        # phantom bar is deduplicated into silence while the fault continues.
        # A guard that reports an ongoing fault once and then goes quiet is
        # the same "cannot see what it is guarding" failure this check exists
        # to fix. Recurrence is the signal here, so it must survive dedup.
        findings.append(
            f"{symbol}: WEEKEND-{weekend[-1]} BAR on a weekday-only series — "
            f"{len(weekend)} of {len(dates)} rows, latest {shown}. "
            f"A Sunday bar becomes Monday's predecessor, so the published "
            f"1-bar change is measured against a session that never happened."
        )
    return findings


# Check G — carried-forward backfill bars.
#
# The late-backfill writer fills yesterday's hole around midday with a bar
# whose OHLC is plausible but whose VOLUME is an exact copy of the previous
# session's. The hole was a Check C finding that morning; the fill CLEARS it,
# so the monitor read a fabricated bar as an improvement. This check makes the
# fill itself a finding.
#
# The predicate is the CLUSTER, not the repeat. Over every volume-bearing weekday cache (16 series, ~14 years): 225 dates
# carry a same-day volume repeat somewhere, almost all on a single instrument
# — a coincidence. Exactly four dates cluster at >= 3 instruments, and three
# of them are known fault dates (n=9, n=9 and n=8 instruments; the fourth,
# n=3, predates Carrac's collection and is plausibly the same fault
# upstream). Four unrelated commodities do
# not repeat Friday's volume on the same Monday by chance.
#
# Per-symbol bar-shape predicates were measured first and REJECTED: "volume
# repeat AND fewer than three distinct OHLC values" catches 1 of 5 on
# a known fault day (GOLD fails it), and a weekend/holiday
# gap condition misses mid-week fills (the rotating set means a Tuesday hole
# gets filled with Monday's volume at gap=1).
#
# STATED BLIND SPOT: instruments publishing volume 0 throughout (FX pairs,
# PLATINUM, DXY) are unreadable by any volume test — their half of a fill day
# is visible only as the Check C MISSING finding the morning before the fill.
CARRIED_MIN_INSTRUMENTS = 3


def carried_forward_bars(symbol_vols, today=None):
    """Flag dates where >= 3 weekday instruments repeat the prior volume.

    ``symbol_vols`` maps symbol -> [(date, volume), ...] in file order.
    Returns ONE finding per fault date, keyed ``BOARD:CARRIED-<date>`` by
    main()'s dedup rule — the fill recurs daily on a rotating instrument set,
    so each date must be a genuinely new alert key (the Check D lesson),
    while the same date must not re-page every morning.
    """
    by_date = {}
    for symbol, rows in sorted(symbol_vols.items()):
        if len(rows) < MIN_ROWS_FOR_WEEKEND_VERDICT:
            continue
        # Weekday instruments only, judged by the series' own shape (same
        # measurement as Check D). Crypto volumes are real on weekends too and
        # its repeats are coincidences that must not help two commodities
        # across the cluster line.
        weekend = sum(1 for d, _ in rows if d.weekday() >= 5)
        if weekend / len(rows) > WEEKEND_TRADER_RATIO:
            continue
        for i in range(1, len(rows)):
            d, vol = rows[i]
            # vol > 0 is load-bearing: an all-zero-volume series repeats 0
            # forever and would flag itself daily.
            if vol > 0 and vol == rows[i - 1][1]:
                by_date.setdefault(d, []).append(symbol)

    findings = []
    for d in sorted(by_date):
        symbols = sorted(by_date[d])
        if len(symbols) < CARRIED_MIN_INSTRUMENTS:
            continue
        findings.append(
            f"BOARD: CARRIED-{d} — {len(symbols)} weekday instruments' {d} "
            f"bar repeats the previous session's exact volume "
            f"({', '.join(symbols)}). That is a backfilled hole carrying a "
            f"copied bar, not a recorded session — the next day's published "
            f"1-bar change is measured against it, and the hole it filled "
            f"clears Check C, so without this finding the fill reads as an "
            f"improvement."
        )
    return findings


def _weekdays_between(a, b):
    """Count of Mon-Fri dates strictly between a and b. Stdlib, loop bounded
    by the gap size (worst real case so far: 1129 days)."""
    n = 0
    d = a + datetime.timedelta(days=1)
    while d < b:
        if d.weekday() < 5:
            n += 1
        d += datetime.timedelta(days=1)
    return n


def interior_gap_finding(symbol, dates):
    """Check A: first interior gap worth reporting for this series, or None.

    Two rules. The calendar rule (> GAP_DAYS) applies to every series and is
    unchanged. The weekday rule (> GAP_WEEKDAYS missing weekday sessions)
    applies only to weekday-only, non-foreign-calendar series — see the
    GAP_WEEKDAYS comment for why the two cannot be one rule. One finding per
    symbol, like the original inline check: the first gap already says the
    series needs a human, and a 14-year series with a bad stretch would
    otherwise flood the report.
    """
    if symbol in KNOWN_LEGITIMATE_GAPS:
        return None
    weekday_rule = (
        symbol not in FOREIGN_SESSION_CALENDARS
        and len(dates) >= MIN_ROWS_FOR_WEEKEND_VERDICT
        and sum(1 for d in dates if d.weekday() >= 5) / len(dates)
            <= WEEKEND_TRADER_RATIO
    )
    for i in range(1, len(dates)):
        delta = (dates[i] - dates[i - 1]).days
        if delta > GAP_DAYS:
            return (
                f"{symbol}: GAP {dates[i-1]} -> {dates[i]} ({delta}d), "
                f"{len(dates)} rows"
            )
        if weekday_rule and delta > GAP_WEEKDAYS:
            missed = _weekdays_between(dates[i - 1], dates[i])
            if missed > GAP_WEEKDAYS:
                return (
                    f"{symbol}: GAP {dates[i-1]} -> {dates[i]} "
                    f"({missed} weekday sessions, {delta}d), "
                    f"{len(dates)} rows"
                )
    return None


def recent_session_gaps(symbol_dates, today=None):
    """Symbols missing a day the rest of the board agrees was a session.

    Check A's GAP_DAYS=45 was sized for a 1129-day hole. It cannot see a
    ONE-day hole — and when nine commodity/FX caches miss one day, the public
    brief compares across the hole and overstates the move (a +0.72% day
    reads as +5.6%) while Check A reports "healthy", correctly by its own
    rules.

    A single symbol CANNOT distinguish a real hole from a public holiday:
    both are just a missing weekday, and recognising holidays needs an
    exchange calendar this script deliberately does not carry (stdlib only,
    runs even when the app is down). The BOARD can distinguish them —
    on a holiday nobody has the day, on a fetch failure most symbols do.
    So the cohort votes, and a naive day-count never becomes noise.

    Crypto trades weekends and would swamp the vote, so only symbols with
    no weekend bars in the window are given one.
    """
    today = today or datetime.date.today()
    window_start = today - datetime.timedelta(days=SESSION_WINDOW_DAYS)

    recent = {}
    for symbol, dates in symbol_dates.items():
        days = sorted(d for d in dates if d >= window_start)
        if days:
            recent[symbol] = days

    # VOTING and CHECKING are separate populations, and conflating them is a
    # blind spot: only clean 5-day-week symbols may define what a session is,
    # but EVERY symbol must then be measured against that definition.
    #
    # GOLD, EURUSD, EURHUF and USDCHF all carry occasional Sunday bars. If
    # cohort membership also gated reporting, every one of them would be
    # silently exempt — including GOLD, the symbol this check exists for.
    # A positive control on a pure weekday-only fetch would still pass, so
    # test with the real history, Sunday bars included.
    #
    # Crypto needs no special case here: it has bars on every weekday, so it
    # is never missing a session and never produces a finding.
    voters = {s: d for s, d in recent.items()
              if not any(x.weekday() >= 5 for x in d)}
    if len(voters) < MIN_SESSION_VOTERS:
        return []          # too few to hold a vote — say nothing, not "healthy"

    tally = {}
    for days in voters.values():
        for d in set(days):
            tally[d] = tally.get(d, 0) + 1
    quorum = len(voters) * SESSION_QUORUM
    sessions = {d for d, n in tally.items() if n >= quorum}

    findings = []
    for symbol, days in sorted(recent.items()):
        if symbol in KNOWN_LEGITIMATE_GAPS or symbol in FOREIGN_SESSION_CALENDARS:
            continue
        have = set(days)
        # Bound by the symbol's OWN span: days before its history starts, or
        # after its tail, are a coverage/staleness question (Check A and the
        # app's own freshness check), not an interior hole.
        missing = sorted(d for d in sessions - have
                         if days[0] < d < days[-1])
        if missing:
            shown = ", ".join(str(d) for d in missing[:3])
            findings.append(
                f"{symbol}: MISSING SESSION {shown}"
                f"{' +%d more' % (len(missing) - 3) if len(missing) > 3 else ''} "
                f"({tally[missing[0]]}/{len(voters)} peers have it), tail {days[-1]}"
            )
    return findings


def scan(data_dir=None):
    """Return (findings, row_counts) for every daily cache."""
    data_dir = data_dir or DATA_DIR
    findings, counts = [], {}

    # --- Instrument master: reference data for Checks E, F and the
    # --- no-real-bars verdict below --------------------------------------
    # One read, used by everything that needs to know what a
    # symbol IS. When it cannot be trusted we fall back to the hardcoded list and
    # the manifest — and SAY SO, because a guard quietly running on a different
    # input than you think is how these checks went blind in the first place.
    # Read BEFORE the file loop because the all-synthetic exemption needs `derived` per file, while the
    # rows are being read.
    records = load_instrument_records(data_dir)
    delisted = None
    if records is None:
        derived = None
        store_tracked = None
        print("  NOTE: no usable _instrument_master.json — Checks E/F fell back "
              "to the hardcoded derived list and the tracked manifest. The "
              "reference layer is not being consulted; if this line persists, "
              "the precompute cycle stopped writing the store.")
    else:
        derived = derived_from_records(records)
        store_tracked = tracked_from_records(records)
        delisted = delisted_from_records(records)
        print(f"  instrument master: {len(records)} records, "
              f"{len(derived)} derived, {len(store_tracked)} tracked, "
              f"{len(delisted)} delisted")
    derived_set = DERIVED_SERIES if derived is None else derived

    symbol_dates = {}
    # The newest bar that records actual trading, per symbol. Kept
    # SEPARATE from symbol_dates rather than filtered out of it, because Checks
    # A/C/D/E all reason about which DATES exist — dropping synthetic rows there
    # would punch holes those checks would then report as gaps.
    symbol_real_tail = {}
    # Check G reads volumes, which no date-based structure carries.
    symbol_vols = {}
    # When the file was last written, so Check F can tell a
    # collector that runs and gains nothing from one that never runs.
    symbol_written = {}

    for name in sorted(os.listdir(data_dir)):
        if not name.endswith("_daily.csv"):
            continue
        symbol = name[: -len("_daily.csv")]
        path = os.path.join(data_dir, name)
        try:
            symbol_written[symbol] = datetime.date.fromtimestamp(
                os.path.getmtime(path))
        except OSError:
            pass

        # The date is always the FIRST column, but its header varies: crypto
        # frames name the index "timestamp", stock/forex/index frames leave it
        # unnamed (pandas writes a bare leading comma). Read positionally.
        try:
            with open(path, newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader, None) or []
                rows = [r for r in reader if r]
        except Exception as exc:
            findings.append(f"{symbol}: UNREADABLE ({exc})")
            continue

        # OHLCV is looked up BY NAME — column count varies (BTC carries a
        # trailing taker_buy_base). Only the date is positional.
        col = {n.strip().lower(): i for i, n in enumerate(header)}
        ohlcv = [col.get(c) for c in ("open", "high", "low", "close", "volume")]
        can_judge_rows = all(i is not None for i in ohlcv)

        if not rows:
            findings.append(f"{symbol}: EMPTY cache file")
            continue

        dates = []
        real_dates = []
        vols = []
        recent_bars = []  # Check H judges only this window
        ohlc_cutoff = datetime.date.today() - datetime.timedelta(days=30)
        for r in rows:
            raw = r[0][:10]
            if len(raw) != 10:
                continue
            try:
                d = datetime.date.fromisoformat(raw)
            except ValueError:
                continue
            dates.append(d)
            if can_judge_rows:
                try:
                    fields = [r[i] for i in ohlcv]
                except IndexError:
                    continue  # short row — unreadable, so not judged either way
                if not is_synthetic_row(*fields):
                    real_dates.append(d)
                try:
                    vols.append((d, float(fields[4])))
                except (TypeError, ValueError):
                    vols.append((d, 0.0))  # matches is_synthetic_row's fallback
                if d >= ohlc_cutoff:
                    try:
                        recent_bars.append((d, *(float(v) for v in fields[:4])))
                    except (TypeError, ValueError):
                        pass
        findings.extend(ohlc_bar_violations(symbol, recent_bars))
        if not dates:
            findings.append(f"{symbol}: no parseable dates ({len(rows)} rows)")
            continue

        counts[symbol] = len(dates)
        symbol_dates[symbol] = dates
        # A fabricated bar is only recognisable RELATIVE to real ones, so a
        # series with no real bars at all cannot be judged by shape.
        #
        # Falling back to the newest bar of any kind for such a series assumes
        # that flat-and-volumeless is its normal encoding. True for the derived ratio
        # feeds, which publish one computed value a day exactly that way and are
        # alive. False, and unfalsifiable from the bars, for a file that is
        # fabrication end to end (AGIX carried 761 such rows). The reference
        # layer answers instead, and a symbol nothing declares to be a
        # computed series gets a finding of its own instead of a free pass.
        if real_dates:
            symbol_real_tail[symbol] = max(real_dates)
        elif symbol in derived_set:
            symbol_real_tail[symbol] = max(dates)
        elif can_judge_rows:
            findings.append(
                f"{symbol}: NO-REAL-BARS — all {len(dates)} rows are flat OHLC "
                f"with zero volume, and nothing routes this as a derived series. "
                f"The file runs to {max(dates)}, so every date-based check reads "
                f"it as healthy; what it is being updated WITH is the problem."
            )
        if vols:
            symbol_vols[symbol] = vols

        # --- Check A: interior gaps (see interior_gap_finding) ----------
        gap_finding = interior_gap_finding(symbol, dates)
        if gap_finding:
            findings.append(gap_finding)

    # --- Check C: one-session holes, decided by cohort vote -------------
    findings.extend(recent_session_gaps(symbol_dates))

    # --- Check D: weekend bars where there is no weekend session --------
    findings.extend(phantom_weekend_bars(symbol_dates))

    # --- Check E: derived series that stopped advancing -----------------
    findings.extend(frozen_derived_series(symbol_dates, derived=derived))

    # --- Check G: carried-forward backfill bars -------------------------
    findings.extend(carried_forward_bars(symbol_vols))

    # --- Check F: any TRACKED symbol that quietly stopped collecting ----
    # Prefer the store's tracking_state; the manifest stays as the fallback so
    # losing the store degrades Check F rather than disabling it.
    tracked = store_tracked if store_tracked else load_tracked_symbols(data_dir)
    if tracked is None:
        # Say so out loud. Silently checking everything would flood the alert
        # with leftover files; silently checking nothing would hide a real
        # dead feed. Neither should happen without a line in the log.
        print("  NOTE: no usable _tracked_symbols.json — Check F skipped "
              "(it cannot tell a dead feed from a retired symbol without it)")
    else:
        findings.extend(frozen_series(symbol_dates, tracked=tracked,
                                      real_tail=symbol_real_tail,
                                      derived=derived, delisted=delisted,
                                      written=symbol_written))

    return findings, counts


def load_baseline():
    try:
        with open(BASELINE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_baseline(baseline):
    try:
        os.makedirs(os.path.dirname(BASELINE_FILE), exist_ok=True)
        tmp = BASELINE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(baseline, fh, indent=2, sort_keys=True)
        os.replace(tmp, BASELINE_FILE)
    except Exception as exc:
        print(f"WARN: could not persist baseline: {exc}")


def notify(title, body, priority, tags) -> bool:
    """Send an alert. Returns True only if it actually reached the server.

    The caller MUST honour the return value: alerts here are
    transition-based, so recording a finding as known after a failed send
    does not delay the alert, it destroys it: a real row collapse and genuine
    session holes would be filed as "already known" while the send was timing
    out, and neither would ever page.

    The failure modes are correlated, which is what makes this sharp: the
    same outage that stops the send is often the thing worth alerting about.

    An unset NTFY_BASE returns False here rather than posting to a
    malformed URL. That is the SAFE direction precisely because of the rule
    above — a False keeps the finding un-filed, so it alerts on the next run
    once the variable is back, instead of being recorded as delivered.
    """
    if not NTFY_BASE:
        print("WARN: NTFY_BASE unset — alert NOT sent and NOT recorded as "
              "known, so it will fire again next run")
        return False
    try:
        req = urllib.request.Request(
            f"{NTFY_BASE}/{NTFY_CARRAC_TOPIC}",
            data=body.encode()[:3500],
            headers={"Title": title, "Priority": priority, "Tags": tags},
        )
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as exc:
        print(f"WARN: ntfy failed, alert NOT recorded so it retries next run: {exc}")
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--reset-baseline", metavar="SYM[,SYM...]|all",
        help="clear the sticky high-water mark for these symbols after a legitimate shrink")
    args = parser.parse_args()

    if not os.path.isdir(DATA_DIR):
        print(f"ERROR: data dir not found: {DATA_DIR}")
        return 2

    # `--reset-baseline SYM[,SYM...]` (or `all`) after a *legitimate* shrink.
    # The high-water mark is deliberately sticky, so a series that correctly
    # gets shorter reports a collapse until its mark is cleared. Real case:
    # TOTAL/BTC.D/... sat at 636 days only because a 270-day orphan block had
    # been welded in; the correct length is a 366-day window, so the drop to
    # 366 is the correction landing, not a collapse.
    if args.reset_baseline:
        baseline = load_baseline()
        targets = args.reset_baseline
        if targets == "all":
            baseline = {}
            print("baseline cleared for ALL symbols")
        else:
            for sym in targets.split(","):
                sym = sym.strip().upper()
                if baseline.pop(sym, None) is not None:
                    print(f"baseline cleared for {sym}")
                else:
                    print(f"no baseline entry for {sym}")
        save_baseline(baseline)
        return 0

    findings, counts = scan()
    baseline = load_baseline()

    # --- Check B: row-count drop vs high-water mark ---------------
    # The store is read a second time here (scan() reads it too but keeps it
    # internal): Check B needs the delisted set for the delisted-symbol exemption,
    # and re-reading one JSON file is cheaper than widening scan()'s return
    # contract, which the tests pin.
    records = load_instrument_records(DATA_DIR)
    delisted = delisted_from_records(records) if records is not None else None
    findings.extend(
        rows_collapsed_findings(counts, baseline, delisted=delisted))

    save_baseline(baseline)

    # State is the SET of findings, not a healthy/degraded flag. With a binary
    # flag, any pre-existing finding pins the state to "degraded" and a brand
    # new corruption would never fire an alert -- exactly the blind spot that
    # let BTC rot for a week. Keyed by symbol + finding type so day-to-day
    # numeric drift in one finding does not re-alert.
    def key_of(f):
        symbol, _, rest = f.partition(":")
        kind = rest.strip().split(" ")[0]  # GAP / ROWS / EMPTY / UNREADABLE / no
        return f"{symbol}:{kind}"

    current_keys = {key_of(f) for f in findings}
    try:
        with open(STATE_FILE) as fh:
            previous_keys = set(json.load(fh))
    except Exception:
        previous_keys = set()

    new_keys = current_keys - previous_keys
    cleared_keys = previous_keys - current_keys

    # What we PERSIST is not simply what we found — it is what we
    # found AND successfully announced. A finding whose alert never left the
    # host stays unrecorded so the next run treats it as new and tries again.
    persist_keys = set(current_keys)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if findings:
        print(f"[{stamp}] cache integrity: DEGRADED ({len(findings)} findings, "
              f"{len(new_keys)} new)")
        for f in findings:
            print(f"  {f}")
        if new_keys:
            body = "\n".join(f for f in findings if key_of(f) in new_keys)[:3500]
            if not notify(f"Carrac cache integrity: {len(new_keys)} NEW finding(s)",
                          body, "high", "warning"):
                persist_keys -= new_keys        # unannounced -> retry next run
    else:
        print(f"[{stamp}] cache integrity: healthy ({len(counts)} caches checked)")

    if cleared_keys and not new_keys:
        if not notify("Carrac cache integrity improved",
                      "Cleared: " + ", ".join(sorted(cleared_keys))[:3000],
                      "default", "white_check_mark"):
            persist_keys |= cleared_keys        # recovery unannounced -> retry

    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as fh:
            json.dump(sorted(persist_keys), fh)
    except OSError:
        pass

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
