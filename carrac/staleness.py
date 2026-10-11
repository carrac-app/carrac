"""Staleness vocabulary — the one place the reason codes are defined.

A leaf module on purpose: ``carrac.formatters`` and the MCP tools need to ask
"is this asset's history dead?" without importing ``carrac.precompute`` (heavy,
and the import would run backwards).

``describe_staleness`` — which builds these blocks — lives next to
``_is_history_stale`` in ``carrac.precompute``, because agreeing with that
function is the whole point of it.

``synthetic_bar_mask`` lives HERE rather than beside it, because it now has
three consumers on opposite sides of the system — the read guard
(``precompute._last_real_bar``), the write guard (``CSVCache.save``) and the
purge script — and ``csv_cache`` cannot import ``precompute`` without the
import running backwards. One predicate, imported three times, is the whole
point: "what counts as a real bar" must not be decided in
one place and assumed in another.
"""

import pandas as pd

def synthetic_bar_mask(df) -> 'pd.Series':
    """Row mask: bars that record no trading — OHLC identical AND zero volume.

    Both halves are required, and each rules out a different real thing:

    * a flat bar with real volume is a genuine no-range session;
    * a zero-volume bar that moved is a venue under-reporting turnover.

    Only together do they mean "nothing happened here" — the shape
    ``inject_synthetic_candle`` produces from a single live price, and the
    shape a dead upstream pair hands back when it repeats its last known
    price forever.

    Returns an all-False mask when the frame cannot be judged (missing OHLC or
    volume columns). An unreadable frame must never be declared fabricated.

    CAUTION for callers: this predicate alone does not mean "fabricated". A
    fabricated bar is only recognisable RELATIVE to real ones — the derived
    ratio feeds (ALTBTC, TOTAL3BTC) publish one computed value a day as all
    four OHLC fields with volume 0 and are perfectly alive. So every caller
    needs an answer for the series in which EVERY row matches.

    That answer is ``instrument_master.is_derived_series(symbol)``, and it is
    reference data on purpose: inferring it from
    the bars (all rows flat, therefore this must be the encoding) is
    unfalsifiable. A caller that reaches this case and cannot name its
    symbol has no basis to decide and must not guess.

    The three callers, all agreeing: ``precompute._last_real_bar`` (read),
    ``CSVCache.save`` (write), ``scripts/purge_synthetic_bars.py`` (cleanup).
    ``scripts/cache_integrity_check.py`` holds a stdlib copy of both rules —
    it cannot import this module — pinned by tests/test_derived_series_
    exemption.py and tests/test_cache_frozen_series.py.
    """
    cols = ('open', 'high', 'low', 'close', 'volume')
    if not all(c in df.columns for c in cols):
        return pd.Series(False, index=df.index)
    op, hi, lo, cl, vol = (pd.to_numeric(df[x], errors='coerce') for x in cols)
    return (vol.fillna(0) == 0) & (op == hi) & (hi == lo) & (lo == cl)


# The history stopped. Nothing derived from price bars means anything.
STALE_NO_RECENT_BARS = 'no_recent_bars'

# The bars are current; today's live tick was refused as implausible against
# them. Deliberately NOT the same thing — a real 50% move trips this guard, and
# an asset having a violent day must not silently vanish from the board.
STALE_PRICE_REJECTED = 'price_rejected'

# The instrument is on the curated delisted list. Distinct
# from STALE_NO_RECENT_BARS on purpose: "no bars since <date>" describes a
# symptom and reads as a Carrac fault, while "delisted" is the reason and is a
# fact about the instrument. A delisted instrument
# would otherwise sit in the stale-data alarm indefinitely as a suspected
# collection failure — this reason code separates the two.
#
# It never implies the history is gone. Every stored bar is retained and every
# historical surface still reads them; this gates what is published as CURRENT.
STALE_DELISTED = 'delisted'

# There is no history at all — the asset never entered the precompute universe.
# `data_stale` is WRITTEN by precompute, so an asset it never
# processed cannot carry a flag; staleness was modelled as "old data" and "no
# data" fell straight through. FTM (zero OHLCV rows) was served with a live
# price and a 362% 24h change, unmarked. Absence has to be a state, not a gap.
STALE_NO_HISTORY = 'no_history'


def no_history_block(symbol: str | None = None) -> dict:
    """The staleness block for an asset with no price history at all.

    Deliberately does not claim the asset is delisted or gone — the honest
    statement is that Carrac has nothing to read, which is also true when the
    precompute cache is merely cold. Overclaiming here would be its own
    fabrication.
    """
    who = f"{symbol} " if symbol else ""
    return {
        'stale': True,
        'reason': STALE_NO_HISTORY,
        'last_bar_date': None,
        'age_days': None,
        'note': (
            f"No price history is available for {who}on Carrac, so no trend, "
            f"momentum, level or structure reading is published for it. Any "
            f"quoted price comes from the live feed and is not corroborated "
            f"against stored bars."
        ),
    }


def delisted_block(symbol: str | None = None) -> dict | None:
    """The staleness block for a curated-delisted instrument, or None.

    The publishing half of the lifecycle override. Returns None for anything not on
    ``instrument_master.LIFECYCLE_OVERRIDES``, so a caller can use it as a
    first-choice check and fall through to the ordinary staleness path.

    The note is written for a reader with no context, like the others here: it
    has to be impossible to read as "reduced data" and equally impossible to
    read as "the bars were deleted". Where the curated entry names a successor
    ticker it is included, because "AGIX is delisted" and "AGIX became FET" send
    a reader to very different next actions.
    """
    if not symbol:
        return None
    try:
        import instrument_master
        entry = instrument_master.lifecycle_override_for(symbol)
    except Exception:  # noqa: BLE001
        # Fail OPEN to the ordinary staleness path — a broken lookup must not
        # suppress the no_recent_bars block that would otherwise fire.
        return None
    if not entry or entry.get('status') != 'delisted':
        return None

    effective = entry.get('effective_date')
    successor = entry.get('successor')
    tail = (
        f" The instrument continued under the ticker {successor}."
        if successor else
        " Carrac has no remaining source for it."
    )
    return {
        'stale': True,
        'reason': STALE_DELISTED,
        'last_bar_date': effective,
        'age_days': None,
        'delisting_reason': entry.get('reason'),
        'successor': successor,
        'note': (
            f"{symbol} is recorded as delisted, with its last collected bar "
            f"dated {effective}. No trend, momentum, level or structure reading "
            f"is published for it, and any quoted price is not corroborated "
            f"against a live market.{tail} Stored history is retained in full "
            f"and remains available to charts and backtests."
        ),
    }


def history_is_dead(data_stale) -> bool:
    """True when an asset's price history stopped updating.

    The predicate that gates suppression: drop derived facts, drop the asset
    from ranked screens. ``STALE_PRICE_REJECTED`` returns False — that asset is
    flagged so the caller can distrust the quoted price, and keeps everything
    its (current) bars support.
    """
    if not data_stale:
        return False
    if data_stale is True:  # legacy boolean flag, predates the reason codes
        return True
    return (data_stale or {}).get('reason') in (
        STALE_NO_RECENT_BARS, STALE_NO_HISTORY, STALE_DELISTED)
