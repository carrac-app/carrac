"""
CSV file caching for OHLCV data.

Provides multi-process-safe CSV persistence for historical price data,
plus JSON persistence for market cap cache (survives container restarts).

Writes go through a temp-file + ``os.replace`` (atomic rename), so a reader
never observes a partially written file and ``load()`` needs no lock. See
``CSVCache._atomic_write_csv`` for the failure mode this prevents.
"""

import csv
import numpy as np
import pandas as pd
import json
import logging
import os
import re
import tempfile
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from carrac.staleness import synthetic_bar_mask
from instrument_master import is_delisted, is_derived_series

logger = logging.getLogger('carrac.csv_cache')

# Dangerous substrings/characters that must never appear in a ticker symbol
# used as part of a filename stem. Path separators, null byte, dot-dot.
# Everything else (dots, equals, carets etc. used by crypto/stock/forex
# tickers) is allowed — the resolved-path check at the bottom of get_path
# is the authoritative safety net.
_FORBIDDEN_SYMBOL_CHARS = ('/', '\\', '\x00')


# ── Weekend-bar write breadcrumb ─────────────────────────────────
# Weekday-only Yahoo/Stooq instruments (GOLD/SILVER/OIL/COPPER/NATGAS/
# PLATINUM/DXY) can pick up a phantom Sunday-dated row in their daily CSV that
# upstream does not have. It is not caused by Yahoo .normalize() UTC shift,
# ETF/spot multipliers, the display-only _inject_live_candle live-candle
# builder, or the empty-fetch/rate-limit path. The writer is database.py's
# get_ohlcv_data, which merges stooq.fetch() output and saves it verbatim; it
# is reached from three callers.
#
# Neither existing guard can see this class of defect:
# `_missing_business_days` (database.py) uses pd.bdate_range, which SKIPS
# weekends, so an EXTRA weekend bar is invisible to it by construction; and
# cache_integrity_check.py explicitly filters weekend dates out. Both guards
# look at the same defect from one side only (missing bars), never the other
# (surplus bars).
#
# The write path is instrumented to record who is writing such a row.
# Observation only — the breadcrumb never blocks the save. Crypto is exempt (it legitimately trades
# weekends); every other daily-timeframe write is checked.
_WEEKEND_BREADCRUMB_PREFIX = 'mcd:weekendbar:'
_WEEKEND_BREADCRUMB_TTL = 14 * 24 * 3600   # 14 days — survives a promote


def _get_redis():
    """Reuse the same Redis singleton as the rest of the cache layer."""
    try:
        from carrac.redis_cache import get_redis
        return get_redis()
    except Exception:
        return None


# The "does this series trade weekends" rule, in ONE place.
#
# It lives here rather than in scripts/purge_weekend_bars.py because there are
# now two consumers with the same question and opposite blast radii: the purge
# script deletes accumulated rows, and CSVCache.save drops incoming ones. Two
# copies of a rule this load-bearing would drift, and the drift would be
# silent — so the script imports these.
#
# Asking "are there ANY weekend rows" would be fooled by the exact defect being
# guarded. Asking "what SHARE" is not. Measured on real data the two populations do
# not come close:
#
#     phantom (the weekend-bar set) 0.0009 .. 0.0236
#     ---------------- empty band ----------------
#     real 24/7 crypto and FX        0.1195 .. 0.2869
#
# Two orders of magnitude with nothing in between, so the threshold is not
# load-bearing.
WEEKEND_TRADER_RATIO = 0.10
MIN_ROWS_FOR_WEEKEND_VERDICT = 60


def trades_weekends(df: 'pd.DataFrame') -> bool:
    """True if this series legitimately has weekend sessions.

    Conservative by construction: a frame too short to judge returns True, so
    both callers refuse to act rather than guess. Both of them destroy data
    when they are wrong.
    """
    if len(df) < MIN_ROWS_FOR_WEEKEND_VERDICT:
        return True
    weekend = int((df.index.weekday >= 5).sum())
    return weekend / len(df) > WEEKEND_TRADER_RATIO


def _existing_dates(cache_path: 'Path') -> set:
    """Dates already on disk, read positionally without a full pandas parse.

    Mirrors scripts/cache_integrity_check.py's approach: the date is always
    the first column, but its header varies (crypto frames name the index
    "timestamp", stock/forex/index frames leave it unnamed), so read it
    positionally rather than by column name.
    """
    dates = set()
    try:
        with open(cache_path, newline='') as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                if not row:
                    continue
                raw = row[0][:10]
                if len(raw) == 10:
                    try:
                        dates.add(datetime.strptime(raw, '%Y-%m-%d').date())
                    except ValueError:
                        pass
    except OSError:
        pass
    return dates


def _existing_volumes(cache_path: 'Path') -> dict:
    """date -> volume for what is already on disk, read positionally.

    The companion to ``_existing_dates``: the carried-volume check
    needs the VALUE that was stored last time, not just that the date existed,
    because the whole re-scoped rule is "did this survive the next fetch".
    Volume is the LAST column in every frame this cache writes, the same way
    the date is always the first.
    """
    out = {}
    try:
        with open(cache_path, newline='') as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                if not row or len(row) < 2:
                    continue
                raw = row[0][:10]
                if len(raw) != 10:
                    continue
                try:
                    out[datetime.strptime(raw, '%Y-%m-%d').date()] = float(row[-1])
                except ValueError:
                    pass
    except OSError:
        pass
    return out


# A carried volume is only worth paging about once it has SURVIVED
# a refetch, and only while it is recent enough to be actionable.
#
# Why survival, not the write: `YahooClient.fetch_daily` ignores its start_date
# and always asks for range=2y, so the merge's keep='last' rewrites the whole
# overlapping history every fetch. A bar written mid-session is therefore
# repaired automatically after settlement, and a beacon that fires on the write
# fires before the repair can happen: an alarm that fires on normal operation,
# which is the noisy-guard failure this guard exists to avoid.
#
# The window is what keeps the OTHER half honest. Yahoo itself still serves a
# carried volume for some historical dates across most commodity series. Those are the source's own values, not our
# fabrication, and a beacon on them would never stop and never be actionable
# (a repair pass would re-fetch the same number forever). Seven days is long
# enough for a settle cycle to have happened several times over and short
# enough that anything still carrying is a live problem, not history.
_CARRIED_SURVIVAL_DAYS = 7
_CARRIED_BREADCRUMB_PREFIX = 'mcd:carriedvol:'
_CARRIED_BREADCRUMB_TTL = 24 * 3600   # at most one page per symbol+date per day


def _carried_already_reported(symbol: str, day: str) -> bool:
    """True when this (symbol, date) was already paged for inside the TTL.

    Without this the beacon re-fires on every save — the precompute cycle
    rewrites these frames roughly every 30 minutes, so a genuine week-long
    carry would produce hundreds of identical events. Fails toward REPORTING:
    no Redis means no dedup means the alarm still reaches a human.
    """
    r = _get_redis()
    if not r:
        return False
    key = f'{_CARRIED_BREADCRUMB_PREFIX}{symbol.upper()}:{day}'
    try:
        if r.get(key):
            return True
        r.setex(key, _CARRIED_BREADCRUMB_TTL, '1')
    except Exception:
        return False
    return False


def _record_weekend_bar_breadcrumb(symbol: str, offending_dates: list) -> None:
    """Capture who is about to write a NEW weekend-dated row for a weekday
    instrument — full call stack, symbol, dates, timestamp. To both Redis
    (survives a promote, mirrors the mcd:gap:outcome idiom in database.py)
    and logger.error (reaches the error tracker via the Sentry logging integration).
    """
    stack = ''.join(traceback.format_stack()[:-1])  # drop this frame itself
    dates_str = [d.isoformat() for d in offending_dates]
    # %-args, not an f-string — the error tracker groups on the log
    # TEMPLATE, and a symbol or date baked into it opened one issue per
    # symbol per weekend date (16 of 62 "new issues" in one week).
    logger.error(
        "weekend-bar write: %s is about to gain %d new weekend-dated "
        "daily row(s) %s — writer is database.py get_ohlcv_data (saving "
        "stooq.fetch() output verbatim)\n%s",
        symbol.upper(), len(offending_dates), dates_str, stack,
    )
    r = _get_redis()
    if not r:
        return
    try:
        payload = json.dumps({
            'symbol': symbol.upper(),
            'dates': dates_str,
            'timestamp': datetime.now().isoformat(),
            'stack': stack,
        })
        r.setex(f'{_WEEKEND_BREADCRUMB_PREFIX}{symbol.upper()}',
                _WEEKEND_BREADCRUMB_TTL, payload)
    except Exception:
        pass


class CSVCache:
    """CSV-based cache, safe for concurrent multi-process access.

    Writes are atomic (temp file + os.replace) and guarded against
    catastrophic shrinkage; see ``_atomic_write_csv`` and ``save``.
    """

    # A write keeping less than this share of the rows already on disk is
    # rejected as a probable upstream read failure.
    SHRINK_FLOOR = 0.5
    # Below this many existing rows the cache is still bootstrapping and the
    # guard would only get in the way.
    SHRINK_GUARD_MIN_ROWS = 20

    def __init__(self, cache_dir: str = 'data'):
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Dynamic symbol cache directory (5min TTL, on-demand only)
        self.dynamic_cache_dir = self.cache_dir / 'dynamic'
        self.dynamic_cache_dir.mkdir(parents=True, exist_ok=True)

    def get_path(self, symbol: str, timeframe: str = 'daily') -> Path:
        """Get cache file path for symbol and timeframe.

        Rejects symbols containing path separators, null bytes, or ``..``
        so the filename can never escape ``self.cache_dir``.
        """
        if not isinstance(symbol, str) or not symbol or len(symbol) > 40:
            raise ValueError(f"unsafe cache symbol: {symbol!r}")
        if '..' in symbol or any(c in symbol for c in _FORBIDDEN_SYMBOL_CHARS):
            raise ValueError(f"unsafe cache symbol: {symbol!r}")
        if not isinstance(timeframe, str) or not re.fullmatch(r'[a-z0-9_]{1,16}', timeframe):
            raise ValueError(f"unsafe cache timeframe: {timeframe!r}")
        safe_symbol = symbol.upper().replace('.', '_').replace('^', '')
        path = (self.cache_dir / f"{safe_symbol}_{timeframe}.csv").resolve()
        # Final safety net: resolved path must live under cache_dir.
        try:
            path.relative_to(self.cache_dir)
        except ValueError as exc:
            raise ValueError(f"cache path escaped cache_dir: {path}") from exc
        return path

    def load(self, symbol: str, timeframe: str = 'daily') -> Optional[pd.DataFrame]:
        """Load cached data from CSV if exists."""
        cache_path = self.get_path(symbol, timeframe)

        if not cache_path.exists():
            return None

        try:
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            # Ensure index is DatetimeIndex (parse_dates can fail silently)
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            logger.debug(f"Loaded {len(df)} cached {timeframe} rows for {symbol} (latest: {df.index[-1]})")
            return df
        except Exception as e:
            logger.warning(f"Error loading {timeframe} cache for {symbol}: {e}")
            return None

    @staticmethod
    def _atomic_write_csv(cache_path: Path, df: pd.DataFrame) -> None:
        """Write ``df`` to ``cache_path`` so readers never see a partial file.

        Opening the path with ``'w'`` and *then* taking an ``flock`` is
        unsafe: ``'w'`` truncates on open, so the destructive step happens
        outside the lock, and a ``load()`` that takes no lock could get a valid
        CSV *prefix* (pandas parses it without error, so the truncation is
        silent) or a 0-byte file. A short read that is delta-topped and written
        back then shrinks the history (a daily series losing most of its rows
        and gaining a multi-year hole).

        Writing to a temp file in the same directory and ``os.replace``-ing it
        over the target is atomic on POSIX: a reader opens either the whole old
        file or the whole new one, never a mixture. This is what makes the
        unlocked ``load()`` safe.
        """
        fd, tmp_name = tempfile.mkstemp(
            dir=str(cache_path.parent),
            prefix=f".{cache_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, 'w') as f:
                df.to_csv(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, cache_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _existing_row_count(cache_path: Path) -> Optional[int]:
        """Row count of the file on disk, counted without parsing it."""
        try:
            with open(cache_path, 'rb') as f:
                lines = sum(1 for _ in f)
        except OSError:
            return None
        return max(lines - 1, 0)  # minus header

    def save(self, symbol: str, df: pd.DataFrame, timeframe: str = 'daily',
             allow_shrink: bool = False, is_crypto: bool = False):
        """Atomically save data to the local CSV cache (see _atomic_write_csv).

        Refuses a write that would shrink an established cache below
        ``SHRINK_FLOOR`` of its current size. A collapse like that is never
        normal operation — it is the signature of a failed read upstream:

        * ``_get_crypto_ohlcv`` delta-topping a torn frame (3259 -> 376 rows),
        * the cold-cache branch firing because ``load()`` returned None on a
          transient error and refetching a shallower window (3259 -> 1503),
        * the WS writers' ``df = new_row`` fallback when ``load()`` returns
          None, which would otherwise leave a one-row cache.

        Callers that legitimately replace a cache with a fixed-size window
        (crypto metrics at 365d) pass ``allow_shrink=True``.

        ``is_crypto`` gates the weekend-bar breadcrumb below: pass
        True for any symbol that legitimately trades weekends (crypto, and
        crypto-derived metrics like BTC.D/TOTAL). Every other daily-timeframe
        write is checked for a NEW weekend-dated row before it lands.
        """
        if df.empty:
            return

        # Writer-side validation — a bar offered for an
        # instrument whose status is not tradeable is REJECTED at ingest, not
        # filtered at read. The collection filter already stops the
        # precompute cycle asking for these, so in normal operation this never
        # fires; it exists because precompute is not the only writer (the WS
        # writers, cache_manager backfills and hand-run scripts all land here)
        # and a read-side filter leaves the bad row on disk to be found again
        # by the next guard.
        #
        # DELISTED is refused; SUSPENDED is ACCEPTED, and that asymmetry is the
        # decision this AC asked to have written down. 'suspended' is Binance's
        # BREAK — a halted session, not an ended instrument. A halted pair
        # routinely resumes, and it can receive a CORRECTION to bars already
        # traded while halted; refusing those writes would leave the cache
        # permanently wrong with no path back, for a state the venue itself
        # calls temporary. 'delisted', by contrast, is only ever set by the
        # curated list in instrument_master.LIFECYCLE_OVERRIDES — a human
        # decision with an author and a date — so refusing it cannot be
        # triggered by a transient feed condition.
        #
        # Refusal never touches what is already stored (the survivorship rule):
        # the existing file is left exactly as it is.
        if is_delisted(symbol):
            logger.error(
                "REFUSED %s cache write for %s: the instrument master records "
                "it as DELISTED, so %d incoming row(s) are not persisted. "
                "Stored history is untouched. If this is wrong, the fix is to "
                "remove it from instrument_master.LIFECYCLE_OVERRIDES — not to "
                "bypass this guard.",
                timeframe, symbol, len(df),
            )
            return

        cache_path = self.get_path(symbol, timeframe)

        if not allow_shrink:
            existing = self._existing_row_count(cache_path)
            if (existing is not None
                    and existing >= self.SHRINK_GUARD_MIN_ROWS
                    and len(df) < existing * self.SHRINK_FLOOR):
                logger.error(
                    f"REFUSED {timeframe} cache write for {symbol}: "
                    f"{len(df)} rows would replace {existing} on disk "
                    f"(<{self.SHRINK_FLOOR:.0%}). Upstream read/fetch probably "
                    f"failed; keeping the existing file. Pass allow_shrink=True "
                    f"if this replacement is intentional."
                )
                return

        if timeframe == 'daily':
            df = self._reject_persisted_synthetic_bars(symbol, cache_path, df)
            df = self._reject_zero_ohlc_bars(symbol, cache_path, df)
            df = self._reject_open_outside_range_bars(symbol, cache_path, df)
            if not is_crypto:
                df = self._check_weekend_bar_write(symbol, cache_path, df)
            if df.empty:
                return
            self._record_backdated_fill(symbol, cache_path, df)

        try:
            self._atomic_write_csv(cache_path, df)
            logger.debug(f"Cached {len(df)} {timeframe} rows for {symbol}")
        except Exception as e:
            # This is the OHLCV source of truth. A failed write
            # here means the collector believes it stored a day it did not,
            # and every chart/indicator downstream quietly ages. The sibling
            # refusals above already escalate; a disk/permission fault must
            # not be the one path that only whispers. ERROR reaches the error tracker
            # via the Sentry logging integration.
            logger.error(
                f"OHLCV cache write FAILED for {symbol} {timeframe} "
                f"({len(df)} rows -> {cache_path}): {e}",
                exc_info=True,
            )

    def _reject_persisted_synthetic_bars(self, symbol: str, cache_path: Path,
                                         df: pd.DataFrame) -> pd.DataFrame:
        """Refuse a NEW flat/zero-volume row dated before today.

        Returns the frame to write. Never raises — every failure path returns
        it untouched, because a fault here must not cost the caller its save.

        The date is what makes this safe to enforce. TODAY's flat zero-volume
        row is real: at 00:00 UTC an exchange kline for the new session
        genuinely is open=prev close, H=L=C=open, volume 0, and it is corrected
        as the day trades (BTC carries one every night). A row dated in the
        PAST is not a session in progress — it is an injected candle that got
        persisted, or a dead upstream pair repeating its last known price. On
        prod that produced 761 consecutive fabricated bars on AGIX, 36 on EOS,
        and the rest of that set.

        Two exemptions, both required to avoid deleting real feeds:

        * a DERIVED series with no real bars at all is using this shape as its
          normal encoding (ALTBTC / TOTAL3BTC publish one computed value a day
          as OHLC with volume 0, and are alive). The exemption is not inferred
          from the bars: that would wave through a series that is fabrication
          end to end — the AGIX shape, and the shape of a brand-new file for a
          dead pair. ``is_derived_series`` answers from the routing
          map instead, so the exemption covers the feeds it exists for and
          nothing else;
        * a row already on disk is left alone. Removing accumulated residue is
          scripts/purge_synthetic_bars.py's job, run by hand after reading a
          dry run — never a side effect of an unrelated save.

        Applies to crypto too, unlike the weekend guard: every symbol this was
        found on is crypto.
        """
        try:
            mask = np.asarray(synthetic_bar_mask(df))
            if not mask.any() or (mask.all() and is_derived_series(symbol)):
                return df

            # ONLY the trailing run. The accumulation mechanism appends at the
            # end, and an interior synthetic row must be left alone: deleting
            # one would punch a hole in the series, which is the interior-gap
            # defect (a fresh tail hiding an interior gap forever) — fixing one
            # bug by causing the other. Walk back from the end while the rows
            # are synthetic and stop at the first real one.
            tail = np.zeros(len(mask), dtype=bool)
            for i in range(len(mask) - 1, -1, -1):
                if not mask[i]:
                    break
                tail[i] = True
            if not tail.any():
                return df

            dates = df.index.normalize()
            today = pd.Timestamp(datetime.now().date())
            already = _existing_dates(cache_path)
            is_new = np.array([d.date() not in already for d in dates], dtype=bool)
            drop = tail & np.asarray(dates < today) & is_new
            if not drop.any():
                return df
            offending = [str(d.date()) for d in dates[drop]]
            # %-args, not an f-string — see _record_weekend_bar_breadcrumb
            # For why a symbol baked into the template opens one
            # error-tracker issue per symbol per date instead of one per line.
            shown = f"{offending[:5]}{'...' if len(offending) > 5 else ''}"
            logger.error(
                "synthetic-bar: refused %d synthetic bar(s) for %s — flat OHLC "
                "with zero volume, dated before today: %s. A dead feed "
                "repeating its last price is not history.",
                int(drop.sum()), symbol, shown,
            )
            return df[~drop]
        except Exception as e:
            logger.debug(f"synthetic-bar check failed for {symbol}: {e}")
            return df

    def _reject_zero_ohlc_bars(self, symbol: str, cache_path: Path,
                               df: pd.DataFrame) -> pd.DataFrame:
        """Refuse a NEW past-dated row whose open/high/low is zero
        while the close is a real price.

        Returns the frame to write. Never raises — a fault here must not cost
        the caller its save.

        This is a DIFFERENT malformation from the flat-bar one, and that is why it
        needs its own check: ``synthetic_bar_mask`` requires open == high ==
        low == close, so ``0 / 0 / 0 / 87.06`` matches nothing it looks for.
        The row is not flat, it is partially missing, and it sails through
        every existing guard while poisoning any indicator that reads a range
        — RVOL, VWAP, ATR, the volume profile and every high/low level.

        This is the source's own value, not our coercion: Yahoo has served CL=F
        as open=0.0, high=0.0, low=0.0, close=87.06, volume=0. So it will not self-repair the way carried volumes
        do — the next fetch returns the same zeros.

        The three conditions are the same ones that make the flat-bar rejection
        safe, for the same reasons:

        * ``close > 0`` while a leg is not — a genuinely empty bar (all five
          fields absent) is already dropped upstream by ``dropna(subset=
          ['close'])``, and a series that legitimately quotes zero would quote
          zero on the close too;
        * dated strictly BEFORE today — the session in progress can briefly
          publish a partial bar, and refusing that would lose real data;
        * NEW only — a row already on disk is left exactly as it is. Removing
          accumulated residue is scripts/purge_synthetic_bars.py's job, run by
          hand after a dry run, never a side effect of an unrelated save.
        """
        try:
            cols = ('open', 'high', 'low', 'close')
            if not all(c in df.columns for c in cols):
                return df
            op, hi, lo, cl = (pd.to_numeric(df[c], errors='coerce') for c in cols)
            broken = np.asarray(
                (cl > 0) & ((op <= 0) | (hi <= 0) | (lo <= 0)).fillna(False))
            if not broken.any():
                return df

            dates = df.index.normalize()
            today = pd.Timestamp(datetime.now().date())
            already = _existing_dates(cache_path)
            is_new = np.array([d.date() not in already for d in dates], dtype=bool)
            drop = broken & np.asarray(dates < today) & is_new
            if not drop.any():
                return df

            offending = [str(d.date()) for d in dates[drop]]
            # %-args, not an f-string — same Bugsink-grouping fix
            # as the flat-bar refusal above.
            logger.error(
                "malformed-bar: refused %d malformed bar(s) for %s dated %s — "
                "open/high/low are zero while the close is a real price, so "
                "the row records a quote and no session. It would corrupt "
                "every range-derived reading (RVOL, VWAP, ATR, key levels). "
                "Rows already on disk are untouched; the source is serving "
                "these, so check it before assuming a repair will converge.",
                int(drop.sum()), symbol, offending,
            )
            return df[~drop]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"zero-OHLC check failed for {symbol}: {e}")
            return df

    def _reject_open_outside_range_bars(self, symbol: str, cache_path: Path,
                                        df: pd.DataFrame) -> pd.DataFrame:
        """Refuse a NEW daily row whose open sits outside its own
        ``[low, high]`` by more than 0.1% of close.

        Returns the frame to write. Never raises — a fault here must not cost
        the caller its save.

        The open is a trade inside the session, so ``low <= open <= high``
        holds at every moment of a REAL session — forming bar included. A
        violation is never a partial bar; it is two sessions stitched into one
        row. Observed on real data: tracked equities were written
        1-110 minutes after the NYSE open with FRIDAY's open (byte-exact in
        nearly all) over Monday's forming high/low/close, then served for days
        because the freshness short-circuit treats "a bar exists for today" as
        fresh regardless of correctness.

        Unlike the zero-leg guard this judges TODAY's row too — the
        poison rows were exactly same-day forming bars, and a forming bar with
        a sane open passes untouched. Refusing the row keeps the tail on the
        previous session, so the very next fetch retries; the
        upstream open typically becomes sane within minutes.

        Tolerance is Check H's noise floor (0.1% of close,
        scripts/cache_integrity_check.py): FX feeds routinely leave the high a
        rounding step below the open, and flagging those trains everyone to
        ignore the guard. ``close > 0`` for the same reason as the zero-leg guard —
        negative-yield series must not be refused wholesale. NEW rows only: a
        date already on disk is left as it is, or the corrected row could not
        replace a poisoned one via ``keep='last'``.
        """
        try:
            cols = ('open', 'high', 'low', 'close')
            if not all(c in df.columns for c in cols):
                return df
            op, hi, lo, cl = (pd.to_numeric(df[c], errors='coerce') for c in cols)
            tol = 0.001
            broken = np.asarray(
                ((cl > 0)
                 & (((op - hi) / cl > tol) | ((lo - op) / cl > tol))).fillna(False))
            if not broken.any():
                return df

            dates = df.index.normalize()
            already = _existing_dates(cache_path)
            is_new = np.array([d.date() not in already for d in dates], dtype=bool)
            drop = broken & is_new
            if not drop.any():
                return df

            offending = [str(d.date()) for d in dates[drop]]
            # %-args, not an f-string — this is the line that opened
            # 13 separate error-tracker issues (one per symbol+date) in one week.
            logger.error(
                "open-outside-range: refused %d bar(s) for %s dated %s — the open "
                "sits outside the bar's own [low, high], so the row carries "
                "prices from two different sessions and every indicator "
                "reading it would be wrong. The tail stays on the previous "
                "session and the next fetch retries; if this repeats all "
                "day, the source is serving a stale open (2026-08-24 shape: "
                "Friday's open on Monday's bar).",
                int(drop.sum()), symbol, offending,
            )
            return df[~drop]
        except Exception as e:  # noqa: BLE001
            logger.debug(f"open-range check failed for {symbol}: {e}")
            return df

    def _record_backdated_fill(self, symbol: str, cache_path: Path,
                               df: pd.DataFrame) -> None:
        """Record a NEW daily row landing INSIDE the existing
        on-disk date range — a hole filled after the fact.

        Observation-only, never drops a row: gap repair is a feature, and a
        REAL late bar closing a hole is exactly what the repair path exists to
        do. What this adds is provenance. Check G (the cross-instrument
        carried-forward detector in scripts/cache_integrity_check.py) can only
        say, the morning after, THAT a fabricated fill landed — this records
        WHICH write landed it, at the moment it happens, so a BOARD:CARRIED
        page traces to a log line instead of a theory. Deliberately not a
        Check G twin: one symbol cannot see cross-instrument clustering, so
        detection stays in the cron; this is the write-side flight recorder.

        Strictly interior dates only (min < date < max of what is on disk):
        tail appends are normal delta operation and prepends are the crypto
        HistoryBackfiller extending history backward — both stay silent by
        construction, so routine traffic never logs.

        The error path deliberately does NOT page merely because an arriving
        interior fill repeats the prior session's volume. That condition is
        normal operation: ``YahooClient.fetch_daily`` ignores
        its start_date and always requests range=2y, so ``keep='last'``
        rewrites the overlapping history on every fetch and a mid-session bar
        repairs itself after settlement. Paging on it would be an
        alarm firing on healthy behaviour, which trains you to ignore it.

        So the ERROR now requires the carried value to have SURVIVED: the date
        is already on disk carrying this same volume, is settled (strictly
        before today), and is inside the last ``_CARRIED_SURVIVAL_DAYS``. That
        last bound is not tidiness — Yahoo itself still serves a carried volume
        for some historical dates on most commodity series, so a
        beacon without a window would page forever about the source's own
        values, and any repair pass over them would never converge.

        An honest interior fill still logs at warning, unchanged — that is the
        provenance record Check G's morning-after finding traces back to.
        Volume 0 repeating volume 0 stays exempt: FX/DXY/PLATINUM encode every
        bar that way, the same reason they are Check G's stated blind spot.
        """
        try:
            existing = _existing_dates(cache_path)
            if len(existing) < 2:
                return
            lo, hi = min(existing), max(existing)
            df_sorted = df.sort_index()
            ndates = df_sorted.index.normalize()
            fills = [i for i, ts in enumerate(ndates)
                     if lo < ts.date() < hi and ts.date() not in existing]
            fill_dates = [str(ndates[i].date()) for i in fills]

            # Which recent, SETTLED sessions repeat the prior
            # session's volume — computed over the whole incoming frame, not
            # only over new fills, because the question is now about a value
            # that PERSISTED rather than one that just arrived.
            #
            # `< today` excludes the session in progress, whose volume
            # legitimately reads as whatever has traded so far. Volume 0
            # repeating volume 0 stays exempt: FX/DXY/PLATINUM encode every bar
            # that way, the same reason they are Check G's stated blind spot.
            surviving = []
            if 'volume' in df_sorted.columns and len(df_sorted) > 1:
                vols = df_sorted['volume']
                today = datetime.now().date()
                floor = today - timedelta(days=_CARRIED_SURVIVAL_DAYS)
                candidates = []
                for i in range(1, len(ndates)):
                    day = ndates[i].date()
                    if not (floor <= day < today):
                        continue
                    if vols.iloc[i] == vols.iloc[i - 1] and vols.iloc[i] > 0:
                        candidates.append((i, day))
                if candidates:
                    # The disk IS the state: a date already stored with THIS
                    # same carried value has survived at least one fetch since
                    # it was written. A value that changed self-corrected, which
                    # is the normal case and must stay silent.
                    on_disk = _existing_volumes(cache_path)
                    for i, day in candidates:
                        prior = on_disk.get(day)
                        if prior is None or float(prior) != float(vols.iloc[i]):
                            continue
                        if _carried_already_reported(symbol, str(day)):
                            continue
                        surviving.append(str(day))

            if surviving:
                logger.error(
                    f"carried volume SURVIVED a refetch for {symbol}: "
                    f"{surviving} still repeat the prior session's volume after "
                    f"being stored and re-fetched — the carried-forward "
                    f"signature Check G pages on (BOARD:CARRIED), and no longer "
                    f"explainable as a mid-session write. OHLC may be a real "
                    f"settle; the volume is not a recorded session. Compare the "
                    f"live source before repairing — if the SOURCE serves the "
                    f"same value there is nothing to converge on."
                )
            elif fills:
                # %-args, not an f-string — same Bugsink-grouping
                # fix as the weekend-bar lines (this is that family's third line).
                logger.warning(
                    "backdated fill for %s: interior daily row(s) "
                    "%s written after the fact (gap repair or late upstream "
                    "publish). Recorded for provenance.",
                    symbol, fill_dates,
                )
        except Exception as e:
            logger.debug(f"backdated-fill check failed for {symbol}: {e}")

    def _check_weekend_bar_write(self, symbol: str, cache_path: Path,
                                  df: pd.DataFrame) -> pd.DataFrame:
        """Record a NEW weekend-dated row, and drop it if phantom.

        Returns the frame to actually write. Never raises — a fault in this
        check must not cost the caller its save, so every failure path returns
        the frame untouched.

        Fires only for a weekend date that is NEW — i.e. not already present in
        the file on disk before this write. Re-saving an existing weekend row
        does not re-fire, and does not delete it either: removing accumulated
        history is the purge script's job, run by hand after a dry run, not a
        side effect of an unrelated save.

        The breadcrumb records the writer's stack; it points at
        ``get_ohlcv_data`` in database.py, which merges ``stooq.fetch()``
        output and saves it verbatim, reached from three different callers.
        So this is not observation-only.

        The drop reuses :func:`trades_weekends`, the SAME second exemption the
        purge script applies, because a blunt "non-crypto means no weekend
        rows" is measurably wrong: FX majors trade Sunday evening, and in production
        that blunt rule would have destroyed 6,784 legitimate rows across 35
        symbols. Both conditions must hold before anything is dropped.

        The breadcrumb must not fire unconditionally right before
        the drop below for the SAME ``offending`` dates — that yields
        "about to gain" then "dropped" pairs ~30ms apart, one drop for every
        gain. ``offending`` is drawn
        from ``df``'s own weekend dates (see ``new_weekend`` below), so for
        any established weekday-only series (>= MIN_ROWS_FOR_WEEKEND_VERDICT
        rows — every real weekday-only instrument here: GOLD,
        SILVER, DXY, ...) the drop a few lines later always removes every one
        of them, so a breadcrumb there would report a write that never
        happens. It records only the dates that actually SURVIVE to the
        write: for that established case that is always none, so it stays
        silent there, and it fires for the one case where the row
        genuinely is kept — a series too short for ``trades_weekends`` to
        judge.
        """
        try:
            new_weekend = {ts.date() for ts in df.index if ts.weekday() >= 5}
            if not new_weekend:
                return df
            offending = sorted(new_weekend - _existing_dates(cache_path))
            if not offending:
                return df

            # Silent ONLY for a series confirmed to trade weekends. A frame too
            # short to judge also returns True from trades_weekends() (it is
            # conservative by design), but "unknown" is not "legitimate" — that
            # case still records below, it just is not dropped either.
            if len(df) >= MIN_ROWS_FOR_WEEKEND_VERDICT and trades_weekends(df):
                # This series legitimately has weekend sessions and the row is
                # NOT dropped — so there is nothing for an operator to act on,
                # and this must not page.
                #
                # A breadcrumb here would beacon to the error tracker every Sunday for
                # every FX major, for a row the very next line deliberately
                # keeps. AUDUSD, USDHUF, EURUSD, GBPUSD and USDJPY carry a
                # real Sunday range every week; only the CURRENT Sunday is
                # flat, because FX
                # opens ~21:00 UTC and the bar is a session-in-progress
                # placeholder that fills in as the day trades. Legitimate data,
                # legitimately kept — and a weekly alarm about it is the
                # "alarm nobody reads" that trains you to ignore the ones that
                # matter.
                logger.debug(
                    "weekend-bar gained: %s now has %d new weekend row(s) %s — series trades "
                    "weekends (ratio > %.2f), keeping",
                    symbol.upper(), len(offending),
                    [d.isoformat() for d in offending], WEEKEND_TRADER_RATIO,
                )
                return df

            if trades_weekends(df):
                # Too short to judge — never dropped on a guess (both callers
                # destroy data when they are wrong), so every offending date
                # really does survive to the write. This is the one case the
                # breadcrumb below is for.
                _record_weekend_bar_breadcrumb(symbol, offending)
                return df

            # Weekday-only. `offending` is a subset of df's own weekend dates,
            # so `drop` below always covers every one of them — nothing here
            # survives to the write. `surviving` is computed explicitly
            # (rather than assumed empty) so a future change to either
            # predicate can't silently reintroduce the defect.
            drop = df.index.normalize().isin(pd.to_datetime(list(offending)))
            if not drop.any():
                return df
            surviving = sorted(set(offending) - {d.date() for d in df.index[drop]})
            if surviving:
                _record_weekend_bar_breadcrumb(symbol, surviving)
            logger.error(   # template + args, see _record_weekend_bar_breadcrumb
                "weekend-bar dropped: %d phantom weekend row(s) from %s before "
                "writing: %s. Upstream (Stooq) returned a session this "
                "weekday-only instrument does not have.",
                int(drop.sum()), symbol, [str(d) for d in offending],
            )
            return df[~drop]
        except Exception as e:
            logger.debug(f"weekend-bar check failed for {symbol}: {e}")
            return df

    def _dynamic_path(self, symbol: str, timeframe: str) -> Path:
        if not isinstance(symbol, str) or not symbol or len(symbol) > 40:
            raise ValueError(f"unsafe dynamic cache symbol: {symbol!r}")
        if '..' in symbol or any(c in symbol for c in _FORBIDDEN_SYMBOL_CHARS):
            raise ValueError(f"unsafe dynamic cache symbol: {symbol!r}")
        if not isinstance(timeframe, str) or not re.fullmatch(r'[a-z0-9_]{1,16}', timeframe):
            raise ValueError(f"unsafe dynamic cache timeframe: {timeframe!r}")
        path = (self.dynamic_cache_dir / f"{symbol.upper()}_{timeframe}.csv").resolve()
        try:
            path.relative_to(self.dynamic_cache_dir)
        except ValueError as exc:
            raise ValueError(f"dynamic cache path escaped dir: {path}") from exc
        return path

    def load_dynamic(self, symbol: str, timeframe: str = 'daily') -> Optional[pd.DataFrame]:
        """Load dynamic (non-curated) symbol cache with 5-minute TTL."""
        symbol_upper = symbol.upper()
        cache_path = self._dynamic_path(symbol, timeframe)

        if not cache_path.exists():
            return None

        try:
            mtime = datetime.fromtimestamp(cache_path.stat().st_mtime)
            if (datetime.now() - mtime).total_seconds() < 300:  # 5 min TTL
                df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
                if not df.empty:
                    logger.debug(f"Dynamic cache hit for {symbol_upper} ({len(df)} rows)")
                    return df
        except Exception:
            pass
        return None

    def save_dynamic(self, symbol: str, df: pd.DataFrame, timeframe: str = 'daily'):
        """Save dynamic symbol data to cache."""
        if df.empty:
            return

        symbol_upper = symbol.upper()
        cache_path = self._dynamic_path(symbol, timeframe)
        try:
            self._atomic_write_csv(cache_path, df)
            logger.info(f"Dynamic OHLCV cached: {symbol_upper} ({len(df)} rows)")
        except Exception as e:
            logger.warning(f"Error caching dynamic {symbol_upper}: {e}")


class MarketCapCache:
    """Persistent JSON cache for market cap data (survives container restarts).

    Two-tier caching:
    1. Centralized in-memory cache (1h TTL) — handled by cache_manager
    2. Persistent file cache (6h TTL) — handled by this class
    """

    CACHE_FILE = 'market_caps_cache.json'
    TTL_HOURS = 6  # Refresh 4x daily

    def __init__(self, cache_dir: str = 'data'):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self) -> Path:
        return self.cache_dir / self.CACHE_FILE

    def load(self) -> Optional[Dict]:
        """Load market cap cache from disk if valid (within TTL)."""
        cache_path = self._get_path()

        if not cache_path.exists():
            return None

        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)

            if '_meta' not in data or 'market_caps' not in data:
                logger.warning("Invalid cache structure, ignoring")
                return None

            updated_at = datetime.fromisoformat(data['_meta']['updated_at'])
            age_hours = (datetime.now() - updated_at).total_seconds() / 3600

            if age_hours > self.TTL_HOURS:
                logger.debug(f"Persistent cache expired ({age_hours:.1f}h > {self.TTL_HOURS}h TTL)")
                return None

            logger.debug(f"Loaded persistent cache ({age_hours:.1f}h old, {data['_meta']['coin_count']} coins)")
            return data

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Error loading cache: {e}")
            return None

    def save(self, df: pd.DataFrame, top_coins: List[Dict]) -> None:
        """Save market cap data to persistent cache with file locking."""
        cache_path = self._get_path()

        try:
            market_caps = {}
            for date_idx in df.index:
                date_str = date_idx.strftime('%Y-%m-%d')
                market_caps[date_str] = {
                    col: float(df.loc[date_idx, col])
                    for col in df.columns
                    if pd.notna(df.loc[date_idx, col])
                }

            cache_data = {
                '_meta': {
                    'version': 1,
                    'updated_at': datetime.now().isoformat(),
                    'ttl_hours': self.TTL_HOURS,
                    'coin_count': len(df.columns)
                },
                'top_coins': top_coins,
                'market_caps': market_caps
            }

            # Same atomic-replace discipline as CSVCache._atomic_write_csv:
            # 'w' truncated before the flock was taken, so a concurrent reader
            # could see an empty or half-written market-cap cache.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(cache_path.parent),
                prefix=f".{cache_path.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(cache_data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, cache_path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

            logger.debug(f"Saved persistent cache: {len(df.columns)} coins, {len(df)} days")

        except Exception as e:
            logger.warning(f"Error saving cache: {e}")

    @staticmethod
    def restore_dataframe(data: Dict) -> pd.DataFrame:
        """Restore DataFrame from cached JSON data."""
        market_caps = data['market_caps']
        df = pd.DataFrame(market_caps).T
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        return df
