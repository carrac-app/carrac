"""
Google Trends collector for Quant DB.

Fetches search interest for crypto-related keywords as a retail
sentiment proxy. Values are relative (0-100 per batch), not absolute.

Rate limits: Google is aggressive. ~10 req/min max, 5s sleep between calls.
Resolution: daily for < 269 days, weekly for longer periods.

Schedule: Daily (05:00 UTC).
Backfill: Weekly resolution back to 2020.

Usage:
    python -m quant.collectors.google_trends
    python -m quant.collectors.google_trends --backfill
    python -m quant.collectors.google_trends --dry-run

Stitching note
--------------
Google normalizes every request's values to 0–100 **relative to that
request's own time-window maximum**.  Stitching disjoint windows therefore
creates artificial scale jumps.

This collector uses the overlapping-window re-anchoring technique:

1. Adjacent windows share an *overlap period* (OVERLAP_DAYS days of shared
   calendar dates).
2. ``stitch_overlapping_windows()`` computes a scaling factor for each
   window from the ratio of its mean value to the previous window's mean
   value *in the shared region*, then chain-multiplies those factors so
   that every window is re-expressed on the first window's scale.
3. The rescaled, deduplicated series is stored as a single continuous
   series.

The first (most-recent) window is always kept as-is; all older windows
are rescaled upward or downward to match it.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.google_trends')

# Two batches (max 5 keywords per Google request)
KEYWORD_BATCHES = [
    ['crypto', 'bitcoin', 'ethereum', 'buy bitcoin', 'bitcoin crash'],
    ['bitcoin etf', 'crypto crash', 'solana', 'defi'],
]

# Metric name mapping (keyword → clean metric name)
METRIC_MAP = {
    'crypto': 'GTREND_CRYPTO',
    'bitcoin': 'GTREND_BITCOIN',
    'ethereum': 'GTREND_ETHEREUM',
    'buy bitcoin': 'GTREND_BUY_BITCOIN',
    'bitcoin crash': 'GTREND_BITCOIN_CRASH',
    'bitcoin etf': 'GTREND_BITCOIN_ETF',
    'crypto crash': 'GTREND_CRYPTO_CRASH',
    'solana': 'GTREND_SOLANA',
    'defi': 'GTREND_DEFI',
}

# How many days of overlap between adjacent fetch windows.
# Must be long enough to produce a stable scale estimate, but short enough
# that both windows' overlap region contains non-zero values.
OVERLAP_DAYS = 30

# Minimum number of overlapping data points required for a reliable scale
# factor.  If fewer are found, we fall back to factor=1 (no rescaling).
MIN_OVERLAP_POINTS = 5


# ---------------------------------------------------------------------------
# Pure stitching function (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def stitch_overlapping_windows(
    windows: List[Dict[str, float]],
) -> Dict[str, float]:
    """Re-anchor overlapping Google Trends windows onto a single common scale.

    Google normalises every API response to 0–100 relative to **that
    response's own maximum**, so raw values from different windows are not
    directly comparable.  This function chain-rescales all windows so they
    share the scale of the *first* (most-recent / reference) window.

    Algorithm
    ---------
    Given windows W0, W1, W2, … (W0 is the reference):

    * W0 is kept as-is (scale factor s0 = 1.0).
    * For W1: find dates present in both W0 and W1 (the overlap region).
      Compute s1 = mean(W0[overlap]) / mean(W1[overlap]).
      Scale all W1 values by s1.
    * For W2: find overlap with (already-rescaled) W1.
      Compute s2 = mean(W1_rescaled[overlap]) / mean(W2[overlap]).
      Scale all W2 values by s2.
    * … and so on.

    Where a window's overlap mean is zero (or the overlap is too short) a
    factor of 1.0 is used (no rescaling) to avoid division by zero.

    After rescaling, windows are merged.  Where the same date appears in
    multiple windows the value from the most-recent (lowest-index) window
    takes precedence, so the freshest Google normalisation is always
    authoritative for dates in the overlap region.

    Parameters
    ----------
    windows:
        Ordered list of ``{date_str: value}`` dicts.  ``windows[0]`` is the
        most-recent (reference) window; subsequent entries are progressively
        older.  Adjacent windows **must** share some date keys for re-anchoring
        to work.

    Returns
    -------
    Dict[str, float]
        Merged, rescaled ``{date_str: float}`` series sorted by date.

    Notes
    -----
    * Values are *not* clamped to [0, 100] after rescaling — downstream
      storage can hold floats and clamping would re-introduce the bias this
      fix is designed to remove.
    * The function is pure (no side-effects) and requires no external
      dependencies beyond the standard library.
    """
    if not windows:
        return {}
    if len(windows) == 1:
        return dict(windows[0])

    # Work on copies to avoid mutating the caller's data.
    rescaled: List[Dict[str, float]] = [dict(windows[0])]  # W0 is reference

    for i in range(1, len(windows)):
        prev = rescaled[i - 1]          # already on reference scale
        curr = dict(windows[i])         # raw, needs rescaling

        # Find overlapping dates.
        overlap_dates = sorted(set(prev.keys()) & set(curr.keys()))

        scale_factor = _compute_scale_factor(prev, curr, overlap_dates)

        # Apply scale factor to all dates in this window.
        rescaled_curr = {d: v * scale_factor for d, v in curr.items()}
        rescaled.append(rescaled_curr)

    # Merge: most-recent window (lowest index) wins on shared dates.
    merged: Dict[str, float] = {}
    for window in reversed(rescaled):   # oldest first → newer overwrites
        merged.update(window)

    return dict(sorted(merged.items()))


def _compute_scale_factor(
    prev: Dict[str, float],
    curr: Dict[str, float],
    overlap_dates: List[str],
) -> float:
    """Compute how much to multiply ``curr`` values to match ``prev``'s scale.

    Returns 1.0 if the overlap is too short or the denominator is zero.
    """
    if len(overlap_dates) < MIN_OVERLAP_POINTS:
        logger.debug(
            "Overlap too short (%d < %d points) — using scale factor 1.0",
            len(overlap_dates), MIN_OVERLAP_POINTS,
        )
        return 1.0

    prev_vals = [prev[d] for d in overlap_dates]
    curr_vals = [curr[d] for d in overlap_dates]

    prev_mean = sum(prev_vals) / len(prev_vals)
    curr_mean = sum(curr_vals) / len(curr_vals)

    if curr_mean == 0.0:
        logger.debug("curr overlap mean is 0 — using scale factor 1.0")
        return 1.0

    return prev_mean / curr_mean


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class GoogleTrendsCollector(BaseCollector):
    name = 'google_trends'
    expected_items = list(METRIC_MAP.values())
    expected_items_key = 'metric'

    def collect(self):
        try:
            from pytrends.request import TrendReq
        except ImportError:
            logger.error("pytrends not installed. Run: pip install pytrends")
            raise

        pytrend = TrendReq(hl='en-US', tz=0, timeout=(10, 25))

        if self.backfill:
            self._backfill(pytrend)
        else:
            self._daily(pytrend)

    def _daily(self, pytrend):
        """Fetch recent daily data using two overlapping windows for scale anchoring.

        We fetch:
          - W0: last 90 days  (today-90 → today)   — reference scale
          - W1: prior 60 days (today-120 → today-30) — overlaps W0 by 30 days

        The 30-day overlap lets stitch_overlapping_windows() compute a reliable
        scale factor so any values that fall only in W1 are expressed on W0's
        scale before being stored.

        In practice the daily job is mostly gap-filling for the last few days, so
        stale rows already in the DB (written by a previous run using W0's scale)
        remain consistent with new rows.
        """
        today = datetime.utcnow().date()
        w1_start = today - timedelta(days=90 + 60 - OVERLAP_DAYS)  # = today-120
        w1_end   = today - timedelta(days=90 - OVERLAP_DAYS)        # = today-60

        logger.info(
            "[google_trends] Daily update — W0: last 90d, W1: %s→%s",
            w1_start, w1_end,
        )

        for batch in KEYWORD_BATCHES:
            try:
                # --- Fetch W0 (reference window, last 90 days) ---
                pytrend.build_payload(batch, timeframe='today 3-m', geo='')
                df0 = pytrend.interest_over_time()
                time.sleep(5)  # Be polite with Google

                # --- Fetch W1 (older window with overlap) ---
                tf1 = f'{w1_start} {w1_end}'
                pytrend.build_payload(batch, timeframe=tf1, geo='')
                df1 = pytrend.interest_over_time()
                time.sleep(5)  # Be polite with Google

                if df0.empty:
                    logger.warning(f"  Batch {batch[:2]}...: W0 empty response")
                    continue

                if 'isPartial' in df0.columns:
                    df0 = df0.drop('isPartial', axis=1)
                if 'isPartial' in df1.columns:
                    df1 = df1.drop('isPartial', axis=1)

                rows = self._stitch_and_build_rows(batch, [df0, df1])

                if rows:
                    count = self.upsert_rows(
                        'quant.sentiment', rows,
                        conflict_cols=['ts', 'metric', 'asset'],
                        update_cols=['value', 'source'],
                    )
                    logger.info(
                        f"  Batch {batch[:2]}...: {len(df0)} days (W0), {count} upserted"
                    )

            except Exception as e:
                logger.warning(f"  Batch {batch[:2]}...: {e}")

    def _backfill(self, pytrend):
        """Backfill weekly data in overlapping chunks.

        Each chunk is CHUNK_DAYS wide; adjacent chunks overlap by OVERLAP_DAYS
        so that stitch_overlapping_windows() can chain-rescale them onto a
        single continuous scale before storing.

        We iterate from *most recent to oldest* so that the first window
        (most recent) is always the reference scale — matching what the daily
        job uses — which means backfilled values are expressed on the same
        scale as live daily data.
        """
        CHUNK_DAYS = 250  # keeps Google in weekly-resolution territory

        start_str = self.backfill_start or '2020-01-01'
        start = datetime.strptime(start_str, '%Y-%m-%d').date()
        end = datetime.utcnow().date()

        logger.info(
            "[google_trends] Backfilling from %s (weekly resolution, "
            "overlapping windows, overlap=%dd)",
            start_str, OVERLAP_DAYS,
        )

        # Build list of windows newest→oldest so W0 (index 0) is most recent.
        windows_spec: List[Tuple] = []
        chunk_end = end
        while chunk_end > start:
            chunk_start = max(start, chunk_end - timedelta(days=CHUNK_DAYS))
            windows_spec.append((chunk_start, chunk_end))
            # Move back by (CHUNK_DAYS - OVERLAP_DAYS) so the next window
            # overlaps this one by exactly OVERLAP_DAYS.
            chunk_end = chunk_end - timedelta(days=CHUNK_DAYS - OVERLAP_DAYS)

        logger.info(
            "[google_trends] Backfill: %d overlapping windows", len(windows_spec)
        )

        for batch in KEYWORD_BATCHES:
            dfs: List = []
            for i, (cs, ce) in enumerate(windows_spec):
                tf = f'{cs} {ce}'
                try:
                    pytrend.build_payload(batch, timeframe=tf, geo='')
                    df = pytrend.interest_over_time()
                    if not df.empty and 'isPartial' in df.columns:
                        df = df.drop('isPartial', axis=1)
                    dfs.append(df)
                    logger.debug(
                        "  [%d/%d] %s→%s: %d rows",
                        i + 1, len(windows_spec), cs, ce,
                        len(df) if not df.empty else 0,
                    )
                except Exception as e:
                    logger.warning(
                        f"  Backfill batch {batch[:2]}... window {tf}: {e}"
                    )
                    dfs.append(None)  # placeholder so indices stay aligned

                time.sleep(10)  # Extra polite during backfill

            # Filter out failed fetches, keeping the newest-first ordering.
            valid_dfs = [df for df in dfs if df is not None and not df.empty]
            if not valid_dfs:
                logger.warning(f"  Backfill batch {batch[:2]}...: all windows empty/failed")
                continue

            rows = self._stitch_and_build_rows(batch, valid_dfs)

            if rows:
                count = self.upsert_rows(
                    'quant.sentiment', rows,
                    conflict_cols=['ts', 'metric', 'asset'],
                    update_cols=['value', 'source'],
                )
                logger.info(
                    f"  Backfill {batch[:2]}...: {len(rows)} periods, {count} upserted"
                )

    def _stitch_and_build_rows(self, batch: List[str], dfs: List) -> List[Dict]:
        """Convert a list of DataFrames → per-keyword stitched rows.

        ``dfs[0]`` is the reference window (most recent).  Subsequent frames
        are older and will be rescaled to match ``dfs[0]``'s scale via
        ``stitch_overlapping_windows()``.

        Returns a flat list of row dicts ready for ``upsert_rows()``.
        """
        rows = []
        for keyword in batch:
            # Build one {date_str: value} dict per window, newest first.
            windows: List[Dict[str, float]] = []
            for df in dfs:
                if df is None or df.empty or keyword not in df.columns:
                    continue
                w: Dict[str, float] = {}
                for date_idx, row in df.iterrows():
                    if keyword in row.index:
                        val = float(row[keyword])
                        if val > 0:  # skip Google's structural zeros
                            w[date_idx.strftime('%Y-%m-%d')] = val
                if w:
                    windows.append(w)

            if not windows:
                continue

            stitched = stitch_overlapping_windows(windows)

            metric = METRIC_MAP.get(keyword, f'GTREND_{keyword.upper()}')
            for ts, val in stitched.items():
                rows.append({
                    'ts': ts,
                    'metric': metric,
                    'asset': '_GLOBAL_',
                    'value': round(val, 4),
                    'source': 'google_trends',
                })

        return rows


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Google Trends collector')
    collector = GoogleTrendsCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
