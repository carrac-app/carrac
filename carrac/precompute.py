"""
Background precompute + sentiment recording.

``carrac.create_app()`` owns all request-path setup while this module
owns the out-of-request compute loop.

Module-level objects are imported by both the web container (for on-demand
fallback via ``/api/assets``) and the worker container (which runs
``_precompute_loop`` as its main loop):

    response_cache, _stop_precompute, _stop_sentiment, _backfiller,
    _compute_assets_response, _compute_one_symbol, _compute_single_asset_ltf,
    warmup_cache, start_precompute_thread, _precompute_loop,
    _start_sentiment_scheduler, _record_sentiment_snapshot.

Services (db, consensio, scorers, etc.) are resolved per call via
``get_services()`` so the module works in both Flask (request context or
app context) and non-Flask contexts (worker.py, CLI scripts, morning
engine).

The per-symbol body of the HTF/LTF pass (``_compute_one_symbol``)
can run under a small ``ThreadPoolExecutor`` (size ``PRECOMPUTE_WORKERS``,
default 1 = the exact serial path) — see ``_compute_assets_response`` and the
benchmark note above ``PRECOMPUTE_WORKERS``.
"""

import math
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial

import pandas as pd

from carrac.services import get_services
from carrac.staleness import (
    STALE_NO_RECENT_BARS,
    STALE_PRICE_REJECTED,
    delisted_block,
    synthetic_bar_mask,
)
from carrac.charts.indicators import _ema_pine_compat
# Repo-root module, stdlib-only at module scope. The import direction is
# one-way by design: `instrument_master` must never import
# `carrac`, because the bare-host integrity cron imports it and that package's
# __init__ bootstraps Sentry/PostHog. App code importing IT is fine.
from instrument_master import is_derived_series

logger = logging.getLogger('carrac.precompute')


# ============================================================
# Shared caches + response-cache singleton
# ============================================================
from carrac.caches import (
    get_cached_derivatives as _get_cached_derivatives,
    set_cached_derivatives as _set_cached_derivatives,
    get_cached_rs as _get_cached_rs,
    set_cached_rs as _set_cached_rs,
)
from carrac.redis_cache import ResponseCache

response_cache = ResponseCache()

_stop_precompute = threading.Event()
_stop_sentiment = threading.Event()
_precompute_thread = None
_backfiller = None

_snapshot_lock = threading.Lock()
previous_snapshot = None

_LTF_HOT_TIER_CORE = [s.strip().upper() for s in os.environ.get(
    'LTF_HOT_TIER', 'BTC,ETH,SOL,XRP,DOGE,ADA,AVAX,LINK,DOT,BNB,BTC.D,TOTAL'
).split(',') if s.strip()]

_hot_tier_cache = {'symbols': None, 'ts': 0}

# Volume-profile lookback (in daily bars) for the scanner's confluence S/R
# zones. The VP MUST reflect recent price action: computing over the full
# multi-year daily history anchored BTC's POC at ~$21k / VAL ~$7,752 (years-old
# prices), which surfaced as useless S/R via scanner_get_confluence_zones. ~6 months of
# daily bars is a robust recent value area; tune via this single constant.
_VP_LOOKBACK_BARS = 180

# ============================================================
# Chart-cache warming
# ============================================================
# Sized from the mcd:chartmiss ledger, not guesses. A map of 12 core crypto x
# 1h/4h/1D + watchlist crypto x 1D (cap 60) assumes single-user, 95%-BTC
# traffic; broader use makes the miss mass long-tail: 3D/1W/1M on core symbols, 4h on watchlist crypto
# (FARTCOIN:4h alone = 28 misses), and every non-crypto watchlist symbol at
# 1D/1W (GOLD, OIL, WHEAT, QQQ, SPX...). Sub-hourly TFs stay unwarmed: their
# rebuilds are the slow ones (~5s) and the ledger's sub-hourly mass was the
# frontend preload, which is no longer counted (see routes._cold_build_verdict).
# Non-crypto is warmed at HTF only — those builds read the CSV cache; 1h/4h
# non-crypto would go through Yahoo's shared per-minute budget.
# Measured headroom: 49 keys in 8.2s of the 45s
# budget; most cycles skip fresh keys for free. Live census: 12 core + 15
# watchlist crypto + 27 watchlist non-crypto -> 72 + 30 + 54 = 156 keys.
_CHART_WARM_CORE_TFS = ['1h', '4h', '1D', '3D', '1W', '1M']
_CHART_WARM_WATCHLIST_TFS = ['1D', '4h']
_CHART_WARM_NONCRYPTO_TFS = ['1D', '1W']
_CHART_WARM_MAX_KEYS = 200
# Mirrors the frontend's own days= value per mode (static/js/chart-loading.js:
# `var maxBars = (mode === 'ltf') ? 2500 : 1500;`) so a warmed payload has the
# same depth a real browser load would get. The cache KEY itself doesn't
# encode `days` (routes.py's `_cache_suffix` has no days component — see
# the SWR cache), but a too-shallow warmed payload would still under-serve a
# user who scrolls back in history.
_CHART_WARM_LTF_DAYS = 2500
_CHART_WARM_HTF_DAYS = 1500

# Lazily-built Flask app used ONLY to replay a chart request via
# _run_chart_refresh's app.test_request_context(...) — see
# _get_chart_warm_app below. Cached at module level: built at most once per
# worker process.
_chart_warm_app = None


def _compute_vp_levels(df_1d):
    """POC/VAH/VAL from the most recent ``_VP_LOOKBACK_BARS`` daily bars.

    The windowing is the whole point — a full-history daily profile produces a
    value area anchored to ancient prices. Returns ``None`` on empty/None input
    or any computation error (the caller then treats VP as absent).
    """
    if df_1d is None or getattr(df_1d, 'empty', True):
        return None
    try:
        from volume_profile import compute_volume_profile
        vp = compute_volume_profile(df_1d.tail(_VP_LOOKBACK_BARS), num_bins=50)
        if vp:
            return {'poc': vp['poc'], 'vah': vp['vah'], 'val': vp['val']}
    except Exception:
        return None
    return None


# ============================================================
# Indicator helpers (single-purpose, pure DataFrame in → dict out)
# ============================================================
def _compute_key_levels(daily_df):
    """Compute previous day/week/month highs/lows and Monday H/L."""
    if daily_df is None or len(daily_df) < 5:
        return None

    idx = pd.to_datetime(daily_df.index)
    last_date = idx[-1].normalize()

    cur_week_mon = last_date - pd.Timedelta(days=last_date.weekday())
    prev_week_mon = cur_week_mon - pd.Timedelta(days=7)

    cur_month_start = last_date.replace(day=1)
    prev_month_start = (cur_month_start - pd.Timedelta(days=1)).replace(day=1)

    result = {}
    prev_day_date = None

    for i in range(len(daily_df) - 1, -1, -1):
        bar_date = idx[i].normalize()
        h, l = daily_df['high'].iloc[i], daily_df['low'].iloc[i]
        if pd.isna(h) or pd.isna(l):
            continue

        if bar_date < last_date:
            if prev_day_date is None:
                prev_day_date = bar_date
            if bar_date == prev_day_date:
                result['pdh'] = max(result.get('pdh', -float('inf')), float(h))
                result['pdl'] = min(result.get('pdl', float('inf')), float(l))

        if bar_date.weekday() == 0:
            if 'mh' not in result:
                result['mh'] = float(h)
                result['ml'] = float(l)
                result['_monday_date'] = bar_date
            elif bar_date == result.get('_monday_date'):
                result['mh'] = max(result['mh'], float(h))
                result['ml'] = min(result['ml'], float(l))

        if prev_week_mon <= bar_date < cur_week_mon:
            result['pwh'] = max(result.get('pwh', -float('inf')), float(h))
            result['pwl'] = min(result.get('pwl', float('inf')), float(l))

        if prev_month_start <= bar_date < cur_month_start:
            result['pmh'] = max(result.get('pmh', -float('inf')), float(h))
            result['pml'] = min(result.get('pml', float('inf')), float(l))

        if bar_date < prev_month_start and prev_day_date is not None and 'mh' in result:
            break

    result.pop('_monday_date', None)
    result = {k: v for k, v in result.items() if v != float('inf') and v != -float('inf')}
    return result if result else None


def _compute_macd(daily_df):
    """Compute MACD from daily close prices."""
    if daily_df.empty or len(daily_df) < 26:
        return None
    _close = daily_df['close']
    _ema12 = _close.ewm(span=12, adjust=False).mean()
    _ema26 = _close.ewm(span=26, adjust=False).mean()
    _macd_line = _ema12 - _ema26
    _macd_signal = _macd_line.ewm(span=9, adjust=False).mean()
    _macd_hist = _macd_line - _macd_signal
    h_cur = float(_macd_hist.iloc[-1])
    h_prev = float(_macd_hist.iloc[-2]) if len(_macd_hist) >= 2 else 0
    return {
        'line': float(_macd_line.iloc[-1]),
        'signal': float(_macd_signal.iloc[-1]),
        'histogram': h_cur,
        'momentum': (
            'bullish_expansion' if h_cur > h_prev and h_cur > 0 else
            'bullish_contraction' if h_cur < h_prev and h_cur > 0 else
            'bearish_expansion' if h_cur < h_prev and h_cur < 0 else
            'bearish_contraction'
        ),
    }


def _compute_ma_levels(daily_df):
    """Compute key MA levels for confluence zones."""
    if daily_df.empty or 'close' not in daily_df.columns:
        return None
    close = daily_df['close']
    n = len(close)
    levels = {}
    # SMA-seeded EMA (Pine-compatible), not pandas'
    # ewm(adjust=False) which seeds from close[0].
    if n >= 21:
        levels['ema21'] = float(_ema_pine_compat(close, 21).iloc[-1])
    if n >= 50:
        levels['ema50'] = float(_ema_pine_compat(close, 50).iloc[-1])
    if n >= 200:
        levels['ema200'] = float(_ema_pine_compat(close, 200).iloc[-1])
        sma = close.rolling(200).mean().iloc[-1]
        if pd.notna(sma):
            levels['sma200'] = float(sma)
    return levels if levels else None


def _compute_cvd_trend(daily_df):
    """Compute CVD spot trend from taker_buy_base."""
    if daily_df.empty or 'taker_buy_base' not in daily_df.columns or len(daily_df) < 20:
        return None
    _tbv = daily_df['taker_buy_base']
    _vol = daily_df['volume']
    _valid = _tbv.notna() & _vol.notna() & (_vol > 0) & (_tbv > 0)
    if _valid.sum() < 20:
        return None
    _delta = (2 * _tbv - _vol).loc[_valid]
    _cvd = _delta.cumsum()
    _slope20 = float(_cvd.iloc[-1] - _cvd.iloc[-20]) if len(_cvd) >= 20 else 0
    _slope5 = float(_cvd.iloc[-1] - _cvd.iloc[-5]) if len(_cvd) >= 5 else 0
    return {
        'direction': 'bullish' if _slope20 > 0 else ('bearish' if _slope20 < 0 else 'neutral'),
        'accelerating': (_slope20 > 0 and _slope5 > _slope20 / 4) or (_slope20 < 0 and _slope5 < _slope20 / 4),
    }


def _compute_cpr_trend(daily_df, is_ltf):
    """Compute CPR trend from monthly pivot direction."""
    if daily_df.empty or is_ltf or len(daily_df) < 60:
        return None
    _daily = daily_df.copy()
    _daily.index = pd.to_datetime(_daily.index)
    _monthly = _daily.resample('ME').agg({'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    if len(_monthly) < 3:
        return None
    _pivots = ((_monthly['high'] + _monthly['low'] + _monthly['close']) / 3).iloc[-4:]
    if len(_pivots) < 3:
        return None
    _diffs = _pivots.diff().dropna()
    _rising = (_diffs > 0).sum()
    _falling = (_diffs < 0).sum()
    return {
        'direction': 'rising' if _rising > _falling else ('falling' if _falling > _rising else 'mixed'),
        'pivots': [round(float(v), 2) for v in _pivots.values],
    }


def _sanitize_floats(obj):
    """Replace NaN/Inf floats with None, recursively.

    One non-finite float anywhere in an asset dict makes the whole
    /api/assets response invalid JSON (bare NaN), which browsers refuse
    to parse — a single bad asset must not brick the endpoint.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    return obj


# ============================================================
# Stale-history alarm
# ============================================================
# _is_history_stale has flagged `data_stale` per asset for a long time, but
# nothing ever READ the flag. Eleven symbols stopped collecting in a single
# Binance delist/migration batch and sat frozen in the universe for eight
# weeks — the signal existed on day one and no one saw it.
#
# Deliberately alarms on the JUMP, not the standing total. There are ~25
# already-stale assets awaiting triage; alarming on the absolute count would
# leave this permanently red, and a guard you learn to ignore is worse than no
# guard. A batch delisting shows up as several NEW stale symbols
# in one cycle, which is exactly what this catches.
_stale_watch = {'count': None, 'symbols': frozenset(), 'seen': frozenset()}

try:
    _STALE_ALARM_JUMP = int(os.getenv('STALE_ALARM_JUMP', '3') or '3')
except (TypeError, ValueError):
    _STALE_ALARM_JUMP = 3
_STALE_ALARM_JUMP = max(1, _STALE_ALARM_JUMP)


def _report_stale_assets(results, is_ltf: bool) -> dict:
    """Log the stale-asset count and alarm on a sudden jump.

    Returns the summary dict so callers (and tests) can assert on it rather
    than scraping log output.
    """
    if is_ltf:
        # The LTF pass covers only the ~21-symbol hot tier; the HTF pass sees
        # the whole universe, so let that one own the number.
        return {}

    seen = frozenset(
        r.get('symbol') for r in results if r.get('symbol')
    )
    stale = frozenset(
        r.get('symbol') for r in results
        if r.get('data_stale') and r.get('symbol')
    )
    prev_symbols = _stale_watch['symbols']
    prev_seen = _stale_watch['seen']
    prev_count = _stale_watch['count']

    # Intersect with what the PREVIOUS cycle actually evaluated.
    # `stale - prev_symbols` alone conflates "went stale" with "was not in the
    # last result set", and the universe is not a constant: after the
    # worker recreate, cycle 1 covered 339 assets and cycle 2 covered
    # 355. Five of the sixteen newcomers were already stale (BGB's newest bar
    # was 21 months old), so they were reported as having gone stale in one
    # cycle, long after the fact. A symbol only WENT stale if it was evaluated
    # last cycle and was healthy then.
    newly = sorted((stale - prev_symbols) & prev_seen)
    recovered = sorted((prev_symbols - stale) & seen)

    _stale_watch['count'] = len(stale)
    _stale_watch['symbols'] = stale
    _stale_watch['seen'] = seen

    logger.info(
        "stale history: %d/%d assets (prev %s), new=%s recovered=%s",
        len(stale), len(results),
        'n/a' if prev_count is None else prev_count,
        ','.join(newly) or '-', ','.join(recovered) or '-',
    )

    summary = {
        'stale_count': len(stale),
        'total': len(results),
        'newly_stale': newly,
        'recovered': recovered,
        'alarmed': False,
    }

    # No baseline on the first cycle after a worker restart — every stale
    # asset would look "new" and fire a false batch alarm.
    if prev_count is None or len(newly) < _STALE_ALARM_JUMP:
        return summary

    summary['alarmed'] = True
    msg = (
        f"{len(newly)} assets went stale in one precompute cycle: "
        f"{', '.join(newly)}. Total stale now {len(stale)}/{len(results)}. "
        f"A batch like this is usually an exchange delisting or ticker "
        f"migration — check whether a renamed successor is already collecting."
    )
    logger.error("STALE-BATCH ALARM: %s", msg)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(msg, level='error')
    except Exception:
        # Never let the alarm path take down the precompute cycle — but the
        # logger.error above has already fired, so this is not a silent loss.
        logger.warning("stale-batch alarm: Sentry capture failed", exc_info=True)
    return summary


def _last_real_bar(daily_df, symbol, now=None):
    """``(timestamp, age_in_days)`` of the last REAL daily bar, or ``(None, None)``.

    A synthetic bar (injected by ``inject_synthetic_candle`` with volume 0)
    must not mask stale history. Two independent things can make a row not
    real, and BOTH have to be excluded:

    * it is dated today — the injected candle for the session in progress;
    * it *is* an injected candle, whatever date it carries.

    Testing only the date would assume that "a previously-injected synthetic
    candle cannot mask the staleness it is being asked to detect". It can.
    Once such a candle is persisted, the next
    day it is dated in the past and sails through the date test, which keeps
    the series looking one day old and authorises the next one — a loop that
    ran 761 consecutive days on AGIX and left five dead assets published as
    live. The date test alone can only ever catch the newest one.

    ``symbol`` is REQUIRED, and required rather than defaulted on purpose. It is
    consulted only in the all-synthetic branch below, where the two possible
    defaults are both silently wrong in a way nobody would notice: rescue a dead
    feed, or declare a live one dead. A caller that cannot name what it is
    judging gets a TypeError instead.
    """
    if daily_df is None or daily_df.empty:
        return None, None
    if now is None:
        now = datetime.now()
    today = pd.Timestamp(now.date())
    real = daily_df[~synthetic_bar_mask(daily_df)]
    # A fabricated bar is only recognisable RELATIVE to real ones, so a series
    # with no real bars at all cannot be judged by shape — the derived ratio
    # feeds (ALTBTC, TOTAL3BTC) publish one computed value a day as OHLC with
    # volume 0 and are perfectly alive.
    #
    # Rescuing ANY such series by inferring "that must be its normal
    # encoding" from the bars is unfalsifiable, and the blind spot it opens is
    # exactly where AGIX sat — all-synthetic from row one, therefore
    # permanently exempt from the guard built to catch it. Reference data
    # answers instead: only a series the routing map calls DERIVED gets the fallback. For
    # anything else, "not one row records trading" is the finding, not a reason
    # to stop looking.
    #
    # Note the branch is still gated on `real.empty` — 'derived' exempts the
    # ENCODING, not the symbol. BTC.D's feed can die behind injected candles like
    # any other, and while it has real bars it is judged on them.
    if real.empty:
        if not is_derived_series(symbol):
            return None, None
        real = daily_df
    idx = pd.to_datetime(real.index)
    before_today = idx[idx < today]
    last_real_date = before_today.max() if len(before_today) else idx.max()
    return last_real_date, (pd.Timestamp(now) - last_real_date).days


def _is_history_stale(daily_df, symbol, now=None, max_age_days: int = 5) -> bool:
    """True if the last REAL daily bar is older than ``max_age_days``.

    A curated-delisted instrument is stale by definition,
    whatever its bars say. This is not belt-and-braces — the two predicates
    must never disagree (``describe_staleness`` delegates here precisely so
    there is one definition), and ``database.py``'s live-price injection guard
    calls THIS one. Without the check, a delisted symbol whose last bar was
    four days old would have a live tick spliced onto dead history, which is
    the shape the injection guard exists to prevent.
    """
    if delisted_block(symbol) is not None:
        return True
    _last, age_days = _last_real_bar(daily_df, symbol, now)
    if _last is None:
        return True
    return age_days > max_age_days


def describe_staleness(daily_df, symbol, now=None, max_age_days: int = 5) -> dict | None:
    """The publishable form of ``_is_history_stale``: a block, or None if fresh.

    Same definition of stale — it delegates — but carrying the date and age a
    consumer needs to say *why* it is refusing to answer. This block ships
    verbatim to every agent-facing surface, so ``note`` is written for an LLM
    reading it cold: it has to be impossible to interpret as "reduced data"
    rather than "no data".

    Two definitions of stale is exactly how the injection guard got its second half, so
    this must never grow its own threshold logic.

    A curated-delisted instrument is answered FIRST and from
    reference data, before any bar is looked at. The age of the bars is not the
    question for it — a delisted symbol whose last bar happens to be four days
    old would otherwise be published as perfectly fresh.
    """
    _delisted = delisted_block(symbol)
    if _delisted is not None:
        return _delisted

    if not _is_history_stale(daily_df, symbol, now=now, max_age_days=max_age_days):
        return None

    block = {
        'stale': True,
        'reason': STALE_NO_RECENT_BARS,
        'last_bar_date': None,
        'age_days': None,
    }
    last_real, age_days = _last_real_bar(daily_df, symbol, now)
    if last_real is None:
        # Two ways to get here and the note has to be true of both: the frame is
        # empty, or it has rows and not one of them records
        # trading. Saying "no history was collected" about a file full of
        # fabricated bars would be the same overclaim in the other direction —
        # rows exist, they just are not evidence of a market.
        block['note'] = (
            'No price history that records actual trading is available for this '
            'asset. No trend, momentum, level or structure reading is available '
            'for it.'
        )
        return block

    block['last_bar_date'] = last_real.strftime('%Y-%m-%d')
    block['age_days'] = int(age_days)
    block['note'] = (
        f"No price data has been collected for this asset since "
        f"{block['last_bar_date']} ({block['age_days']} days ago). Any trend, "
        f"momentum, level or structure reading would describe a market that "
        f"stopped updating, not the market today — so none is published."
    )
    return block


def _drop_delisted(symbols: list) -> list:
    """Remove curated-delisted symbols from a precompute cycle's work list.

    "a delisted symbol stops being collected and stops being
    published, rather than being fetched forever and returning nothing". Both
    halves fall out of this one filter: the symbol is never handed to
    ``_compute_one_symbol``, so no fetch is attempted and no entry reaches the
    assets response.

    RETENTION IS UNTOUCHED. No cache file is deleted,
    no row is dropped, and every historical path — charts, backtests,
    ``quant_get_analogues`` — still reads the full stored series. Pruning dead
    instruments out of history is survivorship bias worth 1-4% of annual
    return, which is a bigger defect than the one being fixed.

    Reads ``instrument_master.LIFECYCLE_OVERRIDES`` (a dict lookup, no I/O, no
    store dependency) rather than the reconciled store, so a missing or stale
    ``_instrument_master.json`` cannot silently un-gate collection.
    """
    try:
        import instrument_master
        dropped = [s for s in symbols if instrument_master.is_delisted(s)]
        if not dropped:
            return symbols
        logger.info(
            "skipping %d delisted symbol(s) this cycle — %s. "
            "History is retained; only collection and publishing stop.",
            len(dropped), ", ".join(sorted(dropped)),
        )
        return [s for s in symbols if not instrument_master.is_delisted(s)]
    except Exception as e:  # noqa: BLE001
        # Fail OPEN: an unusable gate must not empty the work list. Collecting
        # a delisted symbol wastes a fetch; skipping a live one blanks the board.
        logger.error("delisted filter failed, collecting everything: %s",
                     e, exc_info=True)
        return symbols


# ============================================================
# LTF hot tier
# ============================================================
def _get_hot_tier_symbols() -> list:
    """Build the LTF hot tier: core assets + all users' watchlists merged."""
    now = time.time()
    if _hot_tier_cache['symbols'] is not None and now - _hot_tier_cache['ts'] < 300:
        return _hot_tier_cache['symbols']

    core = set(_LTF_HOT_TIER_CORE)

    try:
        from user_db import user_db
        watchlist_syms = user_db.get_all_watchlist_symbols()
    except Exception as e:
        logger.warning(f"Failed to fetch watchlist symbols for hot tier: {e}")
        watchlist_syms = set()

    db = get_services().db
    all_crypto = set(db.crypto_map.keys()) | set(db.crypto_metric_map.keys())
    hot = (core | watchlist_syms) & all_crypto

    result = sorted(hot)
    _hot_tier_cache['symbols'] = result
    _hot_tier_cache['ts'] = now

    logger.info(
        f"LTF hot tier: {len(result)} symbols "
        f"(core={len(core & all_crypto)}, watchlists={len(watchlist_syms & all_crypto)})"
    )
    return result


def _get_watchlist_non_crypto_symbols() -> list:
    """Tracked non-crypto symbols on any user's watchlist.

    "Tracked" = present in one of the provider maps the daily collector
    feeds, so ``get_multi_timeframe_data`` can build the chart from the CSV
    cache without a live fetch. Dynamic (untracked) symbols are excluded on
    purpose: warming them would be a live Yahoo call per key per cycle.
    Sorted so the cap, when it bites, drops deterministically.
    """
    try:
        from user_db import user_db
        watchlist_syms = set(user_db.get_all_watchlist_symbols())
    except Exception as e:
        logger.warning(f"Failed to fetch watchlist symbols for non-crypto warm set: {e}")
        return []
    db = get_services().db
    crypto = set(db.crypto_map.keys()) | set(db.crypto_metric_map.keys())
    tracked = set()
    for attr in ('stock_map', 'etf_map', 'commodity_map', 'index_map', 'forex_map'):
        tracked |= set(getattr(db, attr, {}) or {})
    return sorted((watchlist_syms & tracked) - crypto)


def _build_chart_warm_set() -> list:
    """Bounded (symbol, timeframe) pairs to keep warm in the /api/chart
    response cache.

    Reuses ``_get_hot_tier_symbols`` (core ∪ all users' watchlists, ∩ crypto)
    as the crypto universe, then fans out: core symbols get every browsed
    HTF plus 1h/4h (``_CHART_WARM_CORE_TFS``), watchlist-only crypto gets
    ``_CHART_WARM_WATCHLIST_TFS``, and tracked non-crypto watchlist symbols
    get ``_CHART_WARM_NONCRYPTO_TFS``. Capped at
    ``_CHART_WARM_MAX_KEYS`` total, core first, then watchlist crypto, then
    non-crypto — if the tail would push past the cap, the excess is DROPPED,
    never silently (logged here).
    """
    hot_tier = set(_get_hot_tier_symbols())
    core = sorted(hot_tier & set(_LTF_HOT_TIER_CORE))
    watchlist_only = sorted(hot_tier - set(_LTF_HOT_TIER_CORE))
    non_crypto = _get_watchlist_non_crypto_symbols()

    pairs = [(sym, tf) for sym in core for tf in _CHART_WARM_CORE_TFS]

    dropped = 0
    for sym in watchlist_only:
        for tf in _CHART_WARM_WATCHLIST_TFS:
            if len(pairs) >= _CHART_WARM_MAX_KEYS:
                dropped += 1
                continue
            pairs.append((sym, tf))
    for sym in non_crypto:
        for tf in _CHART_WARM_NONCRYPTO_TFS:
            if len(pairs) >= _CHART_WARM_MAX_KEYS:
                dropped += 1
                continue
            pairs.append((sym, tf))

    if dropped:
        logger.info(
            f"chartwarm: warm set capped at {_CHART_WARM_MAX_KEYS} keys, "
            f"dropped {dropped} key(s) from the watchlist/non-crypto tail"
        )
    return pairs


# ============================================================
# Per-asset + full-assets compute paths
# ============================================================
def _write_ltf_cache(r, cache_key: str, asset_result: dict) -> None:
    """Store one LTF asset result in Redis. A failed write is logged at
    WARNING with the key, not raised: this is a cache, so the next cycle
    recomputes and rewrites it and nothing is lost, but a persistent failure
    must still be visible."""
    try:
        import json as _json
        from carrac.redis_cache import _numpy_serializer
        r.set(cache_key, _json.dumps(asset_result, default=_numpy_serializer), ex=2400)
    except Exception:
        logger.warning("LTF cache write failed for %s", cache_key, exc_info=True)


def _compute_single_asset_ltf(symbol: str) -> dict:
    """Compute LTF indicators for a single asset on-demand (Redis-cached 40 min)."""
    from carrac.caches import _get_redis
    import json as _json

    symbol = symbol.upper()
    cache_key = f'mcd:ltf_asset:{symbol}'

    r = _get_redis()
    if r:
        try:
            raw = r.get(cache_key)
            if raw:
                return _json.loads(raw)
        except Exception:
            pass

    svc = get_services()
    db = svc.db
    consensio = svc.consensio
    rsi_calc = svc.rsi_calc
    ema_ribbon_calc = svc.ema_ribbon_calc
    divergence_detector_daily = svc.divergence_detector_daily
    divergence_detector_weekly = svc.divergence_detector_weekly
    adx_calc = svc.adx_calc
    opportunity_scorer = svc.opportunity_scorer
    balanced_scorer = svc.balanced_scorer
    structure_analyzer = svc.structure_analyzer
    bb_calc = svc.bb_calc

    try:
        asset_info = db.get_asset_info(symbol)
        asset_type = asset_info['type']

        data = db.get_ltf_multi_timeframe_data(symbol)
        tf_data = {'1D': data['1h'], '3D': data['2h'], '1W': data['4h']}

        if tf_data['1D'].empty:
            return None

        consensio_result = consensio.calculate_multi_timeframe(
            tf_data['1D'], tf_data['3D'], tf_data['1W']
        )
        weekly_rsi = rsi_calc.calculate(tf_data['1W']) if not tf_data['1W'].empty else None
        ema_ribbon_data = ema_ribbon_calc.calculate(tf_data['1D']) if not tf_data['1D'].empty else None
        daily_divergence = divergence_detector_daily.detect(tf_data['1D']) if not tf_data['1D'].empty else None
        weekly_divergence = divergence_detector_weekly.detect(tf_data['1W']) if not tf_data['1W'].empty else None
        adx_data = adx_calc.calculate(tf_data['1D']) if not tf_data['1D'].empty else None
        volume_metrics = db.calculate_volume_metrics(tf_data['1D']) if not tf_data['1D'].empty else None
        funding_rate = db.get_funding_rate(symbol) if asset_type == 'crypto' else None
        bb_data = bb_calc.calculate(tf_data['1D']) if not tf_data['1D'].empty else None
        if bb_data is not None and not tf_data['1D'].empty:
            _bb_close = float(tf_data['1D']['close'].iloc[-1])
            _bb_upper = bb_data.get('upper')
            _bb_lower = bb_data.get('lower')
            if _bb_upper is not None and _bb_lower is not None:
                if _bb_close > _bb_upper:
                    bb_data['position'] = 'above_upper'
                elif _bb_close < _bb_lower:
                    bb_data['position'] = 'below_lower'
                else:
                    bb_data['position'] = 'inside'

        consensio_signals = {
            '1d': consensio_result['1D']['signal'],
            '3d': consensio_result['3D']['signal'],
            '1w': consensio_result['1W']['signal']
        }
        rsi_data = {
            'value': weekly_rsi,
            'divergences': {'daily': daily_divergence, 'weekly': weekly_divergence}
        }

        opportunity = opportunity_scorer.calculate_score(
            consensio_signals=consensio_signals, ema_ribbon=ema_ribbon_data,
            rsi_data=rsi_data, price_data=tf_data['1D'],
            volume_metrics=volume_metrics, adx_data=adx_data, funding_rate=funding_rate
        )

        structure_data = structure_analyzer.analyze(tf_data['1D'])
        score_b = balanced_scorer.calculate_score(
            consensio_signals=consensio_signals, ema_ribbon=ema_ribbon_data,
            rsi_data=rsi_data, price_data=tf_data['1D'],
            volume_metrics=volume_metrics, adx_data=adx_data,
            cpr_data=None, structure_data=structure_data
        )

        macd_data = _compute_macd(tf_data['1D'])
        ma_levels = _compute_ma_levels(tf_data['1D'])

        asset_result = {
            'symbol': symbol,
            'name': asset_info['name'],
            'type': asset_type,
            'category': asset_info['category'],
            'latest_price': float(tf_data['1D']['close'].iloc[-1]),
            'positions': consensio_result['total']['positions'],
            'max_positions': consensio_result['total']['max_positions'],
            'consensio': consensio_result,
            'weekly_rsi': weekly_rsi,
            'ema_ribbon': ema_ribbon_data,
            'rsi_divergence': {'daily': daily_divergence, 'weekly': weekly_divergence},
            'adx': adx_data,
            'volume_metrics': volume_metrics,
            'opportunity': opportunity,
            'score_b': score_b,
            'structure': structure_data,
            'bb': bb_data,
            'macd': macd_data,
            'ma_levels': ma_levels,
        }

        try:
            from carrac.scanner.engulfing import (
                detect_engulfing, detect_macd_cross,
                detect_ema_ribbon_flip, detect_alligator_alignment,
            )
            _detectors = [
                detect_engulfing, detect_macd_cross,
                detect_ema_ribbon_flip, detect_alligator_alignment,
            ]
            stream_signals = []
            for tf_key, tf_label in [('1D', '1h'), ('1W', '4h')]:
                tf_df = tf_data.get(tf_key)
                if tf_df is not None and not tf_df.empty:
                    for detector in _detectors:
                        for sig in detector(tf_df):
                            sig['tf'] = tf_label
                            stream_signals.append(sig)
            if stream_signals:
                asset_result['engulfing_signals'] = stream_signals
        except Exception:
            pass

        if r:
            _write_ltf_cache(r, cache_key, asset_result)

        try:
            from carrac.indicator_state import run_incremental_transition
            run_incremental_transition(symbol, asset_result, 'ltf')
        except Exception:
            pass

        return _sanitize_floats(asset_result)

    except Exception as e:
        logger.debug(f"On-demand LTF compute failed for {symbol}: {e}")
        return None


# The ~350-symbol HTF pass (and the hot-tier LTF pass) can run
# the per-symbol body under a small thread pool. DEFAULT IS 1 (bit-identical
# serial path, no executor) per a benchmark: warm-cache
# passes are CPU/GIL-bound (workers=4 measured 957s vs 653-717s serial on a
# box shared with the production worker — no win, real contention), and only a
# genuinely cold cache (hundreds of blocking HTTP round-trips) stands to gain.
# Raising this is a deliberate, supervised experiment: cold starts at 4x call
# rate share the box's Binance IP budget with the other stack's worker.
try:
    PRECOMPUTE_WORKERS = int(os.getenv('PRECOMPUTE_WORKERS', '1') or '1')
except (TypeError, ValueError):
    PRECOMPUTE_WORKERS = 1
PRECOMPUTE_WORKERS = max(1, PRECOMPUTE_WORKERS)


def _compute_one_symbol(
    symbol,
    is_ltf,
    db,
    consensio,
    rsi_calc,
    ema_ribbon_calc,
    trend_analyzer,
    divergence_detector_daily,
    divergence_detector_weekly,
    adx_calc,
    cpr_calc,
    opportunity_scorer,
    research_scorer,
    user_scorer,
    ema_focused_scorer,
    balanced_scorer,
    structure_analyzer,
    bb_calc,
    realtime_prices,
    previous_snapshot,
    funding_hist,
    smart_money_bias_map,
):
    """Compute one symbol's asset_result + facts-bundle delta.

    Extracted verbatim from the formerly-serial per-symbol loop body in
    ``_compute_assets_response`` so it can run under a ThreadPoolExecutor.
    Every dependency is passed explicitly (no closures over mutable state):

      * ``db`` / the scorers, calculators, analyzers are stateless after
        ``__init__`` (only constants assigned there) — safe to share
        read-only across worker threads.
      * ``realtime_prices``, ``previous_snapshot``, ``funding_hist``,
        ``smart_money_bias_map`` are batch-fetched ONCE above the loop in
        ``_compute_assets_response`` and are only ever read here
        (``.get`` / ``[]``), never mutated — so concurrent reads from
        multiple threads are safe.

    Preserves the original per-symbol try/except EXACTLY: any exception
    (including inside the facts-bundle sub-block) is logged and treated as
    "skip this symbol" — never raised to the caller, so one bad symbol can
    never kill the pass, in serial OR threaded mode.

    Returns ``(asset_result_or_None, facts_delta)`` where ``facts_delta`` is
    ``{'elapsed': float, 'ok': int, 'attempted': int}``. The non-atomic
    ``+=`` accumulation onto pass-level counters was the one real hazard of
    naive parallelization — the caller now accumulates these deltas in the
    main thread as results are consumed, instead of mutating shared
    counters from worker threads.
    """
    facts_delta = {'elapsed': 0.0, 'ok': 0, 'attempted': 0}
    try:
        asset_info = db.get_asset_info(symbol)
        asset_type = asset_info['type']

        if is_ltf:
            data = db.get_ltf_multi_timeframe_data(symbol)
            tf_data = {
                '1D': data['1h'],
                '3D': data['2h'],
                '1W': data['4h']
            }
        elif asset_type == 'crypto':
            rt_data = realtime_prices.get(symbol, {})
            rt_price = rt_data.get('price')
            tf_data = db.get_multi_timeframe_data_with_realtime(symbol, rt_price)
        else:
            tf_data = db.get_multi_timeframe_data(symbol)

        if tf_data['1D'].empty:
            return None, facts_delta

        consensio_result = consensio.calculate_multi_timeframe(
            tf_data['1D'], tf_data['3D'], tf_data['1W']
        )
        weekly_rsi = rsi_calc.calculate(tf_data['1W']) if not tf_data['1W'].empty else None
        ema_ribbon_data = ema_ribbon_calc.calculate(tf_data['1D']) if not tf_data['1D'].empty else None
        daily_divergence = divergence_detector_daily.detect(tf_data['1D']) if not tf_data['1D'].empty else None
        weekly_divergence = divergence_detector_weekly.detect(tf_data['1W']) if not tf_data['1W'].empty else None
        adx_data = adx_calc.calculate(tf_data['1D']) if not tf_data['1D'].empty else None
        volume_metrics = db.calculate_volume_metrics(tf_data['1D']) if not tf_data['1D'].empty else None
        funding_rate = db.get_funding_rate(symbol) if asset_type == 'crypto' else None
        # Perp basis (markPrice vs indexPrice, %) —
        # same crypto-only guard as funding_rate, same source fetch cycle.
        perp_basis_pct = db.get_perp_basis(symbol) if asset_type == 'crypto' else None
        cpr_data = cpr_calc.calculate_monthly_cpr(tf_data['1D']) if not tf_data['1D'].empty and not is_ltf else None
        weekly_cpr_data = cpr_calc.calculate_weekly_cpr(tf_data['1D']) if not tf_data['1D'].empty and not is_ltf else None
        daily_cpr_data = cpr_calc.calculate_daily_cpr(tf_data['1D']) if not tf_data['1D'].empty and not is_ltf else None

        consensio_signals = {
            '1d': consensio_result['1D']['signal'],
            '3d': consensio_result['3D']['signal'],
            '1w': consensio_result['1W']['signal']
        }
        rsi_data = {
            'value': weekly_rsi,
            'divergences': {'daily': daily_divergence, 'weekly': weekly_divergence}
        }

        opportunity = opportunity_scorer.calculate_score(
            consensio_signals=consensio_signals, ema_ribbon=ema_ribbon_data,
            rsi_data=rsi_data, price_data=tf_data['1D'],
            volume_metrics=volume_metrics, adx_data=adx_data, funding_rate=funding_rate
        )
        research_opportunity = research_scorer.calculate_score(
            consensio_signals=consensio_signals, ema_ribbon=ema_ribbon_data,
            rsi_data=rsi_data, price_data=tf_data['1D'],
            volume_metrics=volume_metrics, adx_data=adx_data
        )
        user_opportunity = user_scorer.calculate_score(
            consensio_signals=consensio_signals, ema_ribbon=ema_ribbon_data,
            rsi_data=rsi_data, price_data=tf_data['1D'],
            volume_metrics=volume_metrics, adx_data=adx_data, cpr_data=cpr_data
        )
        score_a = ema_focused_scorer.calculate_score(
            consensio_signals=consensio_signals, ema_ribbon=ema_ribbon_data,
            rsi_data=rsi_data, price_data=tf_data['1D'],
            volume_metrics=volume_metrics, adx_data=adx_data, cpr_data=cpr_data
        )
        structure_data = structure_analyzer.analyze(tf_data['1D'])
        bb_data = bb_calc.calculate(tf_data['1D']) if not tf_data['1D'].empty else None
        if bb_data is not None and not tf_data['1D'].empty:
            _bb_close = float(tf_data['1D']['close'].iloc[-1])
            _bb_upper = bb_data.get('upper')
            _bb_lower = bb_data.get('lower')
            if _bb_upper is not None and _bb_lower is not None:
                if _bb_close > _bb_upper:
                    bb_data['position'] = 'above_upper'
                elif _bb_close < _bb_lower:
                    bb_data['position'] = 'below_lower'
                else:
                    bb_data['position'] = 'inside'

        macd_data = _compute_macd(tf_data['1D'])
        ma_levels = _compute_ma_levels(tf_data['1D'])
        cvd_trend = _compute_cvd_trend(tf_data['1D'])
        cpr_trend = _compute_cpr_trend(tf_data['1D'], is_ltf)

        atr_val = None
        atr_pct = None
        try:
            if not tf_data['1D'].empty and len(tf_data['1D']) >= 15:
                from carrac.charts.indicators_extended import _atr
                _atr_series = _atr(tf_data['1D'], period=14)
                if not _atr_series.empty:
                    _last_atr = _atr_series.iloc[-1]
                    if pd.notna(_last_atr):
                        atr_val = float(_last_atr)
                        _atr_price = float(tf_data['1D']['close'].iloc[-1])
                        if _atr_price > 0:
                            atr_pct = atr_val / _atr_price * 100
        except Exception:
            pass

        score_b = balanced_scorer.calculate_score(
            consensio_signals=consensio_signals, ema_ribbon=ema_ribbon_data,
            rsi_data=rsi_data, price_data=tf_data['1D'],
            volume_metrics=volume_metrics, adx_data=adx_data,
            cpr_data=cpr_data, structure_data=structure_data
        )

        asset_result = {
            'symbol': symbol,
            'name': asset_info['name'],
            'type': asset_type,
            'category': asset_info['category'],
            'latest_price': float(tf_data['1D']['close'].iloc[-1]),
            'positions': consensio_result['total']['positions'],
            'max_positions': consensio_result['total']['max_positions'],
            'consensio': consensio_result,
            'weekly_rsi': weekly_rsi,
            'ema_ribbon': ema_ribbon_data,
            'rsi_divergence': {'daily': daily_divergence, 'weekly': weekly_divergence},
            'adx': adx_data,
            'volume_metrics': volume_metrics,
            'cpr': cpr_data,
            'weekly_cpr': weekly_cpr_data,
            'daily_cpr': daily_cpr_data,
            'opportunity': opportunity,
            'research_opportunity': research_opportunity,
            'user_opportunity': user_opportunity,
            'score_a': score_a,
            'score_b': score_b,
            'structure': structure_data,
            'bb': bb_data,
            'macd': macd_data,
            'ma_levels': ma_levels,
            'cvd_trend': cvd_trend,
            'cpr_trend': cpr_trend,
            'atr': atr_val,
            'atr_pct': atr_pct,
        }

        # The flag must carry the date and age, not a bare True: a bare flag
        # was ignored by every serving surface, so a full technical read was
        # shipped off a 57-day-old flatline. The surfaces below act on it.
        _stale_block = describe_staleness(tf_data['1D'], symbol)
        if _stale_block is None and symbol in getattr(db, 'price_injection_rejects', ()):
            _stale_block = {
                'stale': True,
                'reason': STALE_PRICE_REJECTED,
                'note': (
                    "This asset's bars are current, but today's live price was "
                    "refused as implausible against them — treat the quoted "
                    "price as unconfirmed."
                ),
            }
        if _stale_block is not None:
            asset_result['data_stale'] = _stale_block

        if not tf_data['1D'].empty and len(tf_data['1D']) >= 30:
            # Recent-window VP (see _compute_vp_levels) — NOT full history,
            # which anchored POC/VAL to years-old prices.
            _vp = _compute_vp_levels(tf_data['1D'])
            if _vp:
                asset_result['vp_levels'] = _vp

        if not tf_data['1D'].empty and len(tf_data['1D']) >= 5:
            _kl = _compute_key_levels(tf_data['1D'])
            if _kl:
                asset_result['key_levels'] = _kl

        try:
            from carrac.charts.indicators_extended import compute_cockpit_snapshot
            _ck_ext = compute_cockpit_snapshot(tf_data['1D'], tf='1D')
            if _ck_ext:
                asset_result['cockpit_extended'] = _ck_ext
        except Exception:
            pass

        try:
            from carrac.scanner.engulfing import (
                detect_engulfing, detect_macd_cross,
                detect_ema_ribbon_flip, detect_alligator_alignment,
            )
            _detectors = [
                detect_engulfing, detect_macd_cross,
                detect_ema_ribbon_flip, detect_alligator_alignment,
            ]
            stream_signals = []
            if is_ltf:
                _sig_tfs = [('1D', '1h'), ('1W', '4h')]
            else:
                _sig_tfs = [('1D', 'D'), ('3D', '3D'), ('1W', 'W')]
            for tf_key, tf_label in _sig_tfs:
                tf_df = tf_data.get(tf_key)
                if tf_df is not None and not tf_df.empty:
                    for detector in _detectors:
                        for sig in detector(tf_df):
                            sig['tf'] = tf_label
                            stream_signals.append(sig)
            if stream_signals:
                asset_result['engulfing_signals'] = stream_signals
        except Exception:
            pass

        if funding_rate is not None:
            asset_result['funding_rate'] = funding_rate

        if perp_basis_pct is not None:
            asset_result['perp_basis_pct'] = perp_basis_pct

        # Join Hyperliquid smart-money bias (0-100) onto crypto
        # assets for the Scanner's smart-money score modifier. Map is
        # already batch-fetched once per cycle above the loop (no
        # per-asset DB call). Non-crypto / non-covered assets simply get
        # no key, so they score exactly as before.
        if asset_type == 'crypto':
            _sm_bias = smart_money_bias_map.get(symbol)
            if _sm_bias is not None:
                asset_result['smart_money_bias'] = _sm_bias

        if asset_type == 'crypto' and symbol in realtime_prices:
            rt_data = realtime_prices[symbol]
            _rt_price = rt_data.get('price')
            _rt_open = rt_data.get('open')
            _chg_val = None
            if _rt_price is not None:
                if _rt_open:
                    _chg_val = _rt_price - _rt_open
                else:
                    # no 24h open (non-Binance source) — derive from change_pct
                    _pct = rt_data.get('change_pct')
                    if _pct is not None and (1 + _pct / 100) != 0:
                        _chg_val = _rt_price - _rt_price / (1 + _pct / 100)
            asset_result['realtime'] = {
                'price': _rt_price,
                'change_24h': rt_data.get('change_24h'),
                'change_pct': rt_data.get('change_pct'),
                'change_value': _chg_val,
                'open': _rt_open,
                'updated_at': rt_data.get('updated_at'),
                'is_live': True,
                # CoinPaprika enrichment, pure passthrough —
                # None when the symbol has no CoinPaprika coverage.
                'ath_distance_pct': rt_data.get('ath_distance_pct'),
                'change_pct_7d': rt_data.get('change_pct_7d'),
                'change_pct_30d': rt_data.get('change_pct_30d'),
            }
        else:
            _daily = tf_data.get('1D')
            if _daily is not None and len(_daily) >= 2:
                _prev_close = _daily['close'].iloc[-2]
                _curr_close = _daily['close'].iloc[-1]
                if _prev_close and _prev_close != 0:
                    _chg_pct = ((_curr_close - _prev_close) / _prev_close) * 100
                    asset_result['realtime'] = {
                        'price': float(_curr_close),
                        'change_pct': round(_chg_pct, 2),
                        'change_value': float(_curr_close - _prev_close),
                        'is_live': False
                    }
                else:
                    asset_result['realtime'] = None
            else:
                asset_result['realtime'] = None

        prev_data = previous_snapshot.get(symbol, {})
        asset_result['prev_signal_1d'] = prev_data.get('signal_1d')
        asset_result['prev_signal_3d'] = prev_data.get('signal_3d')
        asset_result['prev_signal_1w'] = prev_data.get('signal_1w')
        trend_analysis = trend_analyzer.analyze(asset_result, prev_data)
        asset_result['trend_analysis'] = trend_analysis

        # LTF assets intentionally skip bundle storage because tf_data['1D'] holds 1h bars
        # in LTF mode, which would clobber the daily-resolution bundle.
        if not is_ltf:
            try:
                import time as _time
                from datetime import datetime as _dt, timezone as _tz
                from carrac.facts.wire import build_facts_ctx, store_asset_facts
                from carrac.indicator_eval import rsi_zone as _rsi_zone
                from morning_engine.candle_utils import volume_ratio_closed as _vol_ratio_closed
                _t0 = _time.time()
                # E1: daily RSI block (was weekly_rsi — fixed)
                _daily_rsi = rsi_calc.calculate(tf_data['1D'])
                _rsi_block = {
                    'value': _daily_rsi,
                    'zone': _rsi_zone(_daily_rsi),
                    'divergence': daily_divergence,
                }
                # E3: confluence zones (individually guarded so a failure → zones=None)
                try:
                    from carrac.scanner.confluence import compute_confluences
                    _zones = compute_confluences(
                        asset_result, asset_result['latest_price'], 'long'
                    )
                except Exception:
                    _zones = None
                # E4: weekly RSI block for the weekly ctx
                _weekly_rsi_block = {
                    'value': weekly_rsi,
                    'zone': _rsi_zone(weekly_rsi),
                    'divergence': weekly_divergence,
                }
                # E5: closed-candle volume block — uses last CLOSED daily bar +
                # junk-feed guard (_volume_series_reliable) so silver 16× / corn
                # 0.02× artefacts cannot reach the bundle.
                _now_dt = _dt.now(_tz.utc)
                _now_ms = int(_now_dt.timestamp() * 1000)
                _df_1d = tf_data['1D']
                if not _df_1d.empty and 'volume' in _df_1d.columns:
                    _ts_s = _df_1d.index.astype('datetime64[s]').astype('int64').tolist()
                    _vols = _df_1d['volume'].tolist()
                    _vol_series = [{"time": t, "value": float(v or 0)}
                                   for t, v in zip(_ts_s, _vols)]
                else:
                    _vol_series = []
                _vr = _vol_ratio_closed(_vol_series, 86400, _now_ms)
                _vr_reliable = _vr.get('reliable', False)
                _vr_ratio = _vr['ratio'] if _vr_reliable else None
                if _vr_ratio is not None:
                    if _vr_ratio > 1.5:
                        _vr_signal = 'high'
                    elif _vr_ratio < 0.7:
                        _vr_signal = 'low'
                    else:
                        _vr_signal = 'normal'
                else:
                    _vr_signal = None
                _vol_block = {
                    'rvol': _vr_ratio,
                    'signal': _vr_signal,
                    'reliable': _vr_reliable,
                }
                _facts_ctx = build_facts_ctx(
                    symbol,
                    tf_data['1D'],
                    asset_result['latest_price'],
                    key_levels=asset_result.get('key_levels'),
                    ma_levels=ma_levels,
                    rsi=_rsi_block,
                    volume=_vol_block,
                    zones=_zones,
                    monthly_cpr={'position': cpr_data.get('position')} if cpr_data else None,
                    daily_closes=list(tf_data['1D']['close']),
                    as_of=_now_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    weekly_df=tf_data['1W'],
                    weekly_rsi=_weekly_rsi_block,
                    funding_series=funding_hist.get(symbol),
                    smart_money_bias=smart_money_bias_map.get(symbol),
                    data_stale=asset_result.get('data_stale'),
                )
                facts_delta['attempted'] += 1
                _stored = store_asset_facts(symbol, _facts_ctx)
                if _stored is not None:
                    facts_delta['ok'] += 1
                facts_delta['elapsed'] += _time.time() - _t0
            except Exception as _facts_err:
                logger.warning("facts bundle for %s failed (non-fatal): %s", symbol, _facts_err)

        return _sanitize_floats(asset_result), facts_delta
    except Exception as e:
        logger.warning(f"Error processing {symbol} in background: {e}")
        return None, facts_delta


def _compute_assets_response(mode: str) -> dict:
    """Compute the full /api/assets response (internal function)."""
    global previous_snapshot

    svc = get_services()
    db = svc.db
    consensio = svc.consensio
    rsi_calc = svc.rsi_calc
    ema_ribbon_calc = svc.ema_ribbon_calc
    trend_analyzer = svc.trend_analyzer
    divergence_detector_daily = svc.divergence_detector_daily
    divergence_detector_weekly = svc.divergence_detector_weekly
    adx_calc = svc.adx_calc
    cpr_calc = svc.cpr_calc
    opportunity_scorer = svc.opportunity_scorer
    research_scorer = svc.research_scorer
    user_scorer = svc.user_scorer
    ema_focused_scorer = svc.ema_focused_scorer
    balanced_scorer = svc.balanced_scorer
    structure_analyzer = svc.structure_analyzer
    bb_calc = svc.bb_calc

    is_ltf = mode == 'ltf'
    tf_keys = ('1h', '2h', '4h') if is_ltf else ('1D', '3D', '1W')

    with _snapshot_lock:
        if previous_snapshot is None:
            previous_snapshot = db.load_previous_snapshot()

    should_save = db.should_save_snapshot() if not is_ltf else False

    realtime_prices = db.get_combined_crypto_prices()

    if is_ltf:
        symbols = _get_hot_tier_symbols()
    else:
        symbols = db.get_all_symbols()
        _write_tracked_symbols_manifest(symbols)
        # The instrument master reconciles cache files against the
        # manifest just written, so it belongs here and nowhere else.
        _write_instrument_master()
        # ...and the collection gate goes AFTER both, deliberately. The
        # manifest and the store must keep describing the full universe —
        # dropping a delisted symbol from them would mark its cache file an
        # orphan and lose the very record that explains why it stopped. What
        # is filtered is only what this cycle FETCHES and publishes.
        symbols = _drop_delisted(symbols)

    results = []
    _facts_elapsed = 0.0
    _facts_ok = 0
    _facts_attempted = 0

    # Hoist funding-history fetch ABOVE the per-asset loop (one batched query).
    # Non-crypto assets simply won't appear in this dict, so they get no funding key.
    try:
        from carrac.facts.funding import fetch_funding_history
        _funding_hist = fetch_funding_history(90)
    except Exception:
        _funding_hist = {}

    # Hoist smart-money-bias fetch ABOVE the per-asset loop (one batched query).
    # Non-Hyperliquid assets won't appear in this dict → no on_chain key in bundle.
    try:
        from carrac.facts.on_chain import fetch_smart_money_bias
        _smart_money_bias_map = fetch_smart_money_bias()
    except Exception:
        _smart_money_bias_map = {}

    worker_fn = partial(
        _compute_one_symbol,
        is_ltf=is_ltf,
        db=db,
        consensio=consensio,
        rsi_calc=rsi_calc,
        ema_ribbon_calc=ema_ribbon_calc,
        trend_analyzer=trend_analyzer,
        divergence_detector_daily=divergence_detector_daily,
        divergence_detector_weekly=divergence_detector_weekly,
        adx_calc=adx_calc,
        cpr_calc=cpr_calc,
        opportunity_scorer=opportunity_scorer,
        research_scorer=research_scorer,
        user_scorer=user_scorer,
        ema_focused_scorer=ema_focused_scorer,
        balanced_scorer=balanced_scorer,
        structure_analyzer=structure_analyzer,
        bb_calc=bb_calc,
        realtime_prices=realtime_prices,
        previous_snapshot=previous_snapshot,
        funding_hist=_funding_hist,
        smart_money_bias_map=_smart_money_bias_map,
    )

    workers = max(1, PRECOMPUTE_WORKERS)
    logger.info(
        "%s pass starting: %d symbols, workers=%d",
        'LTF' if is_ltf else 'HTF', len(symbols), workers,
    )

    executor = None
    if workers == 1:
        # PRECOMPUTE_WORKERS=1 is a genuine serial rollback path: a plain
        # builtin map() iterator, no ThreadPoolExecutor instantiated at
        # all, so a stack trace on failure looks exactly like the old
        # serial loop.
        result_iter = map(worker_fn, symbols)
    else:
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='precompute')
        # executor.map preserves submission order, so `results` ends up in
        # the same symbol order as a serial loop — nothing
        # downstream depends on anything but that final order.
        result_iter = executor.map(worker_fn, symbols)

    try:
        for _i, (asset_result, facts_delta) in enumerate(result_iter):
            # Refresh the worker heartbeat every ~20 assets so the
            # 900s TTL never expires during the ~20 min cold-cache warmup.
            # Without this, the heartbeat Docker healthcheck flags the
            # worker unhealthy long before _precompute_loop gets a chance
            # to refresh it itself. It lives in this main-thread
            # result-consumption loop because per-symbol work can run on
            # worker threads.
            if _i % 20 == 0:
                _write_heartbeat()
            if asset_result is not None:
                results.append(asset_result)
            _facts_elapsed += facts_delta['elapsed']
            _facts_ok += facts_delta['ok']
            _facts_attempted += facts_delta['attempted']
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    try:
        from carrac.facts.cost import track_batch_timing
        track_batch_timing("facts", _facts_elapsed)
        logger.info("facts bundles: %d/%d assets stored", _facts_ok, _facts_attempted)
    except Exception:
        pass

    if should_save and results:
        db.save_daily_snapshot(results)
        with _snapshot_lock:
            previous_snapshot = db.load_previous_snapshot()

    # Surface the stale-history count and alarm on a sudden jump.
    try:
        _report_stale_assets(results, is_ltf)
    except Exception:
        logger.warning("stale-asset reporting failed", exc_info=True)

    crypto_count = sum(1 for r in results if r.get('type') == 'crypto')
    stock_count = sum(1 for r in results if r.get('type') in ('stock', 'etf'))
    other_count = len(results) - crypto_count - stock_count

    parts = []
    if crypto_count > 0:
        parts.append(f"{crypto_count} crypto runes")
    if stock_count > 0:
        parts.append(f"{stock_count} stock scrolls")
    if other_count > 0:
        parts.append(f"{other_count} market artifacts")

    fetch_stats = f"The wizard gathered {' + '.join(parts)}!"

    return {
        'assets': results,
        'count': len(results),
        'mode': mode,
        'timeframes': tf_keys,
        'fetch_stats': fetch_stats
    }


# ============================================================
# Background loop + warmup + thread management
# ============================================================
def _write_heartbeat():
    """Write worker heartbeat to Redis (TTL 900s).

    The TTL is 900s, not 300s: the per-iteration refresh runs every 20
    symbols, which can exceed 300s on slow CCXT venues, causing the key to
    expire mid-cycle and triggering an autoheal cascade (worker restart →
    HTF cache goes stale → web /health 503 → web restart → uptime monitors
    flap). 900s gives 3× margin over the worker healthcheck's 600s degraded
    threshold. A failed write is logged at ERROR and not raised (a Redis outage
    must not kill compute); a missing heartbeat is what the healthcheck acts on.
    """
    try:
        import os as _os, json as _json
        from datetime import datetime as _dt
        from carrac.caches import _get_redis
        r = _get_redis()
        if r:
            r.set('mcd:worker:heartbeat', _json.dumps({
                'ts': _dt.now().isoformat(),
                'pid': _os.getpid(),
            }), ex=900)
    except Exception as e:
        logger.error("worker heartbeat write failed; the healthcheck will see a "
                     "stale heartbeat: %s", e, exc_info=True)


def _get_chart_warm_app():
    """Lazily build (once per worker process) the Flask app object needed to
    replay a chart request via ``_run_chart_refresh``'s
    ``app.test_request_context(...)`` (the SWR background-refresh
    mechanism, reused here rather than reinvented).

    The worker process normally never builds a Flask app at all — it calls
    ``init_services()`` directly and skips ``create_app()`` entirely (see
    ``worker.py``'s module docstring). Building it here is side-effect-free:
    ``WORKER_MODE=true`` (set by ``worker.py`` before ``import carrac``)
    makes ``create_app()``'s ``_bootstrap_background_threads`` return
    immediately (no duplicate precompute loop, no signal/atexit handlers),
    and ``init_services()`` is idempotent (no second ServiceContainer) — so
    this just adds routing/extensions, once, cached at module level.
    """
    global _chart_warm_app
    if _chart_warm_app is None:
        try:
            from carrac.asgi import flask_app
            _chart_warm_app = flask_app
        except Exception as e:
            logger.warning(f"chartwarm: could not build Flask app for replay: {e}")
            return None
    return _chart_warm_app


def _chart_warm_lookup_tf(tf: str) -> str:
    """The exact cache key real ``/api/chart`` traffic resolves to for this
    timeframe. Built by routes.py's own ``build_chart_cache_suffix`` since
    A hand-synced copy would drift, and then every warmed key is
    one no request ever reads. tests/test_chart_cache_key_parity.py pins it.

    The frontend always sends ``fmt=col`` (static/js/chart-loading.js), so
    ``columnar=True`` here; warm requests never carry ``?quant=``.
    """
    from carrac.charts.routes import build_chart_cache_suffix
    return tf + build_chart_cache_suffix(tf, columnar=True)


def _warm_chart_cache():
    """Proactively keep the /api/chart response cache warm for a
    bounded hot set, so the FIRST view of a popular asset — not just a
    repeat view — is a ~100ms cache hit instead of a 1.1-6.3s cold rebuild.

    Reuses the SWR background-refresh machinery
    (``get_cached_chart`` / ``_try_acquire_chart_refresh_lock`` /
    ``_run_chart_refresh``) rather than reinventing chart-building or
    cache-key logic. Rebuilds run SEQUENTIALLY, one at a time — the worker
    shares 8 vCPUs with the web containers, and overlapping cold computes
    would starve interactive traffic (an explicit review point).
    Time-budgeted so a slow cycle degrades to "warmed fewer keys" instead of
    delaying the rest of ``_precompute_loop``.

    Kill-switch: ``CHART_WARM_ENABLED`` (default on) is read live — same
    style as this module's other env toggles (``HYPERLIQUID_WS_ENABLED`` in
    worker.py etc.) — so it can be flipped without a restart if a cycle
    misbehaves. ``CHART_WARM_BUDGET_S`` (default 45) bounds wall-clock time
    per cycle.
    """
    if os.environ.get('CHART_WARM_ENABLED', '1').strip().lower() in ('0', 'false', 'no'):
        return

    try:
        budget_s = float(os.environ.get('CHART_WARM_BUDGET_S', '45') or 45)
    except (TypeError, ValueError):
        budget_s = 45.0

    from carrac.caches import get_cached_chart
    from carrac.charts.routes import (
        _try_acquire_chart_refresh_lock,
        _build_chart_refresh_query,
        _run_chart_refresh,
        _FORMING_INTERVAL_MS,
    )

    app = _get_chart_warm_app()
    if app is None:
        return  # already logged by _get_chart_warm_app

    pairs = _build_chart_warm_set()
    t0 = time.monotonic()
    warmed = skipped_fresh = deferred = 0
    budget_hit = False

    for i, (symbol, tf) in enumerate(pairs):
        if time.monotonic() - t0 >= budget_s:
            budget_hit = True
            deferred += len(pairs) - i
            break

        try:
            is_ltf = tf.lower() in _FORMING_INTERVAL_MS
            mode = 'ltf' if is_ltf else 'htf'
            lookup_tf = _chart_warm_lookup_tf(tf)

            cached = get_cached_chart(symbol, lookup_tf, False)
            if cached is not None and cached[1]:
                skipped_fresh += 1
                continue

            cache_key = f"{symbol}:{lookup_tf}:0"
            if not _try_acquire_chart_refresh_lock(cache_key):
                # Already being rebuilt — a real request's own SWR spawn, or
                # a still-running rebuild from a previous warm cycle.
                deferred += 1
                continue

            days_raw = _CHART_WARM_LTF_DAYS if is_ltf else _CHART_WARM_HTF_DAYS
            query = _build_chart_refresh_query(tf, mode, False, True, '', days_raw)
            _run_chart_refresh(app, symbol, query, cache_key)
            warmed += 1
        except Exception as e:
            logger.warning(f"chartwarm: {symbol} {tf} failed: {e}")
            deferred += 1

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "chartwarm: warmed=%d skipped_fresh=%d deferred=%d budget_hit=%s %dms",
        warmed, skipped_fresh, deferred, budget_hit, elapsed_ms,
    )


def _precompute_loop():
    """Background loop that keeps the response cache warm."""
    import push_notifications

    logger.info("Background pre-compute thread started")

    # Guard rail: skip chart-cache warming on this loop's very
    # first iteration. worker.py's own warmup_cache() (the actual "startup
    # warmup phase" — it runs entirely OUTSIDE this loop, before
    # _precompute_loop is ever called) plus this iteration's own cold HTF
    # pass already load the box; give it one full cycle to settle before
    # layering chart rebuilds on top.
    _chart_warm_skip_first = True

    while not _stop_precompute.is_set():
        # Refresh heartbeat at top of every iteration so a slow
        # compute cycle still counts as 'alive'.
        _write_heartbeat()

        for mode in ['htf']:
            if _stop_precompute.is_set():
                break

            try:
                cached = response_cache.get(mode)
                if cached and cached.get('_cache_age', 999) < response_cache.REFRESH_INTERVAL - 60:
                    continue

                if response_cache.is_computing(mode):
                    continue

                logger.info(f"Background pre-computing {mode.upper()} data...")
                response_cache.set_computing(mode, True)

                start_time = datetime.now()
                data = _compute_assets_response(mode)
                elapsed = (datetime.now() - start_time).total_seconds()

                response_cache.set(mode, data)
                response_cache.set_computing(mode, False)

                if mode == 'htf':
                    # Mirror the same pass into the per-symbol
                    # hash so composite readers can HMGET instead of
                    # fetching the whole ~2.1MB blob. Same-pass write keeps
                    # hash and blob from diverging for long; best-effort
                    # (never raises) so a hash failure can't break the
                    # precompute loop.
                    try:
                        from carrac.caches import set_htf_by_symbol_hash
                        set_htf_by_symbol_hash(data)
                    except Exception as htf_hash_err:
                        logger.warning(f"HTF by-symbol hash write failed: {htf_hash_err}")

                logger.info(
                    f"Background pre-compute {mode.upper()} complete: "
                    f"{len(data.get('assets', []))} assets in {elapsed:.1f}s"
                )

                try:
                    from carrac.indicator_state import run_transition_detection
                    transitions = run_transition_detection(data.get('assets', []), mode)
                    if transitions:
                        try:
                            from carrac.indicator_state import check_indicator_alerts
                            check_indicator_alerts(transitions)
                        except Exception as alert_err:
                            logger.warning(f"Indicator alert check error: {alert_err}", exc_info=True)
                except Exception as trans_err:
                    logger.warning(f"Transition detection error: {trans_err}", exc_info=True)

                if mode == 'htf':
                    try:
                        from carrac.indicator_state import check_formula_alerts
                        check_formula_alerts()
                    except Exception as formula_err:
                        logger.warning(f"Formula alert check error: {formula_err}", exc_info=True)

                if mode == 'htf':
                    try:
                        from carrac.indicator_state import check_position_health
                        check_position_health(response_cache)
                    except Exception as pos_err:
                        logger.warning(f"Position monitoring error: {pos_err}", exc_info=True)

                if mode == 'htf':
                    try:
                        from carrac.scanner.scorer import score_all_assets, _build_macro_data
                        from carrac.caches import set_cached_scanner
                        macro = _build_macro_data()
                        scanner_data = score_all_assets(data.get('assets', []), macro)
                        set_cached_scanner(scanner_data)
                        logger.info(f"Scanner scored {len(scanner_data.get('results', []))} assets")
                        try:  # persist universe-wide daily snapshot (self-gated, non-fatal)
                            from carrac.scanner.scorer.history import upsert_daily_snapshot
                            upsert_daily_snapshot(scanner_data, macro.get('regime'))
                        except Exception as hist_err:
                            logger.error("scanner_history: daily snapshot not written: %s", hist_err)
                    except Exception as scan_err:
                        logger.warning(f"Scanner scoring failed: {scan_err}")

                    try:
                        from carrac.plays.monitor import plays_monitor
                        plays_monitor.check_all_active(data)
                    except Exception as play_err:
                        logger.warning(f"Plays monitor error: {play_err}", exc_info=True)

            except Exception as e:
                logger.error(f"Error in background pre-compute ({mode}): {e}")
                response_cache.set_computing(mode, False)

        try:
            _now = time.time()
            if not hasattr(_precompute_loop, '_last_email_flush'):
                _precompute_loop._last_email_flush = _now
            if _now - _precompute_loop._last_email_flush >= push_notifications._EMAIL_DIGEST_INTERVAL:
                push_notifications.flush_email_digests()
                _precompute_loop._last_email_flush = _now
        except Exception as email_err:
            logger.warning(f"Email digest flush error: {email_err}", exc_info=True)

        try:
            cached_htf = response_cache.get('htf')
            if cached_htf:
                prices = {}
                for asset in cached_htf.get('assets', []):
                    symbol = asset.get('symbol')
                    price = asset.get('latest_price')
                    if symbol and price:
                        prices[symbol] = float(price)
                if prices:
                    triggered = push_notifications.check_alerts(prices)
                    if triggered:
                        logger.info(f"Triggered {len(triggered)} price alerts")
        except Exception as alert_err:
            logger.error(f"Error checking price alerts: {alert_err}", exc_info=True)

        try:
            db = get_services().db
            for sym in ['BTC', 'ETH', 'XRP']:
                for period in ['1d', '1h']:
                    oi_key = f"oi:{sym}:{period}"
                    if _get_cached_derivatives(oi_key) is None:
                        oi_data = db.fetch_open_interest_hist(sym, period=period, limit=500) or []
                        _set_cached_derivatives(oi_key, oi_data)

                    usdt_key = f"cvd:{sym}:{period}:usdt"
                    if _get_cached_derivatives(usdt_key) is None:
                        usdt_data = db.fetch_cvd_futures(sym, period=period, limit=500, margin_type='usdt') or []
                        _set_cached_derivatives(usdt_key, usdt_data)

                    coin_key = f"cvd:{sym}:{period}:coin"
                    if _get_cached_derivatives(coin_key) is None:
                        coin_data = db.fetch_cvd_futures(sym, period=period, limit=500, margin_type='coin') or []
                        _set_cached_derivatives(coin_key, coin_data)
                time.sleep(1)
        except Exception as deriv_err:
            logger.warning(f"Derivatives pre-warm error: {deriv_err}", exc_info=True)

        try:
            if _get_cached_rs() is None:
                from carrac.charts.routes import _compute_rs_rankings
                rs_data = _compute_rs_rankings()
                _set_cached_rs(rs_data)
                logger.info(f"RS rankings pre-warmed: {len(rs_data)} assets")
        except Exception as rs_err:
            logger.warning(f"RS pre-warm error: {rs_err}", exc_info=True)

        # Pre-warm the /report/pulse + /report/morning payloads so those pages
        # serve a warm cache (~50ms) instead of recomputing on every request
        # (build_snapshot + double RS rankings for pulse; ~24 multi-timeframe
        # consensio calcs for morning). Self-healing: rebuilds whenever its own
        # cache is missing or older than the refresh interval.
        _stale = response_cache.REFRESH_INTERVAL - 60
        try:
            existing = response_cache.get('pulse_payload')
            if not existing or existing.get('_cache_age', 999) >= _stale:
                from carrac.reports.pulse import build_pulse_payload
                response_cache.set('pulse_payload', build_pulse_payload())
                logger.info("Pre-warmed pulse_payload cache")
        except Exception as pulse_err:
            logger.warning(f"pulse_payload pre-warm error: {pulse_err}", exc_info=True)

        try:
            existing = response_cache.get('morning_board')
            if not existing or existing.get('_cache_age', 999) >= _stale:
                from carrac.reports.routes import build_morning_board
                response_cache.set('morning_board', build_morning_board())
                logger.info("Pre-warmed morning_board cache")
        except Exception as mb_err:
            logger.warning(f"morning_board pre-warm error: {mb_err}", exc_info=True)

        try:
            from carrac.journal.open_mfe_hook import refresh_open_trade_mfe_mae
            refresh_open_trade_mfe_mae()
        except Exception as mfe_hook_err:
            logger.warning(f"open_mfe_hook error: {mfe_hook_err}", exc_info=True)

        # Proactively warm the /api/chart response cache for a
        # bounded hot set. Never allowed to crash this loop — any failure is
        # logged and the loop continues exactly as if the pass hadn't run.
        _chart_warm_elapsed = 0.0
        if _chart_warm_skip_first:
            _chart_warm_skip_first = False
        else:
            try:
                _chart_warm_t0 = time.monotonic()
                _warm_chart_cache()
                _chart_warm_elapsed = time.monotonic() - _chart_warm_t0
            except Exception as chart_warm_err:
                logger.warning(f"chartwarm: pass failed: {chart_warm_err}", exc_info=True)

        # Subtract the warm pass's own runtime from the sleep. The
        # pass can take up to its 45s budget; without this the real cadence
        # is sleep(~60s) + warm(~45s) ≈ 113s — LONGER than the 1h/4h chart
        # TTL, so the warmer could never find its own entries still fresh and
        # would rebuild everything every cycle (65% warmed / 12%
        # skipped_fresh flat, worker pegged at ~105% CPU).
        # Cadence must stay under the shortest warmed TTL for skipped_fresh
        # to dominate in steady state.
        _stop_precompute.wait(timeout=max(10.0, random.uniform(55, 65) - _chart_warm_elapsed))


def start_precompute_thread():
    """Start the background pre-compute thread."""
    global _precompute_thread

    if _precompute_thread and _precompute_thread.is_alive():
        logger.warning("Pre-compute thread already running")
        return

    _stop_precompute.clear()
    _precompute_thread = threading.Thread(
        target=_precompute_loop,
        daemon=True,
        name="PrecomputeThread"
    )
    _precompute_thread.start()


def warmup_cache():
    """Warm up the cache on startup (compute HTF immediately)."""
    logger.info("Warming up response cache (computing HTF data)...")
    try:
        response_cache.set_computing('htf', True)
        start_time = datetime.now()
        data = _compute_assets_response('htf')
        elapsed = (datetime.now() - start_time).total_seconds()
        response_cache.set('htf', data)
        response_cache.set_computing('htf', False)
        try:  # same-pass per-symbol hash mirror (see loop site)
            from carrac.caches import set_htf_by_symbol_hash
            set_htf_by_symbol_hash(data)
        except Exception as htf_hash_err:
            logger.warning(f"HTF by-symbol hash write failed (warmup): {htf_hash_err}")
        logger.info(
            f"Cache warmup complete: {len(data.get('assets', []))} HTF assets in {elapsed:.1f}s"
        )
        try:
            from carrac.scanner.scorer import score_all_assets, _build_macro_data
            from carrac.caches import set_cached_scanner
            macro = _build_macro_data()
            scanner_data = score_all_assets(data.get('assets', []), macro)
            set_cached_scanner(scanner_data)
            logger.info(
                f"Scanner scored {len(scanner_data.get('results', []))} assets (warmup)"
            )
            try:  # persist universe-wide daily snapshot (self-gated, non-fatal)
                from carrac.scanner.scorer.history import upsert_daily_snapshot
                upsert_daily_snapshot(scanner_data, macro.get('regime'))
            except Exception as hist_err:
                logger.error("scanner_history: daily snapshot not written (warmup): %s", hist_err)
        except Exception as scan_err:
            logger.warning(f"Scanner scoring failed during warmup: {scan_err}")
    except Exception as e:
        logger.error(f"Cache warmup failed: {e}")
        response_cache.set_computing('htf', False)


def _startup_warmup():
    """Startup warmup in background thread."""
    warmup_cache()
    start_precompute_thread()


# ============================================================
# Sentiment history scheduler
# ============================================================
TRACKED_SYMBOLS_MANIFEST = '_tracked_symbols.json'


def _write_tracked_symbols_manifest(symbols) -> None:
    """Record the live tracked universe next to the CSV caches.

    ``scripts/cache_integrity_check.py`` runs on the HOST, stdlib
    only, so that it still works when the app is down — which means it cannot
    ask the app what it tracks. Without that list its staleness check judges
    every ``*_daily.csv`` on disk, a typical install has frozen files for
    symbols that are no longer tracked at all. Alerting on those is
    unactionable by construction, and an alarm nobody can act on is how a guard
    becomes noise.

    Written from the FULL (non-LTF) cycle only — the hot tier is a subset and
    would shrink the manifest to a handful of symbols.

    Best-effort by design: the checker treats a missing or stale manifest as
    "cannot judge" and says so, rather than guessing.
    """
    try:
        import json as _json
        import os as _os

        # Same resolution OHLCVDatabase (database.py) uses to site the CSV caches, so the
        # manifest always lands beside the files it describes.
        data_dir = _os.environ.get('DATA_DIR', 'data')
        payload = {
            'symbols': sorted(str(s).upper() for s in symbols),
            'written_at': datetime.now().isoformat(),
        }
        path = _os.path.join(data_dir, TRACKED_SYMBOLS_MANIFEST)
        tmp = f"{path}.tmp"
        with open(tmp, 'w') as fh:
            _json.dump(payload, fh)
        _os.replace(tmp, path)   # atomic — the host reads this concurrently
    except Exception:  # noqa: BLE001
        logger.error("tracked-symbols manifest write failed", exc_info=True)


# a human-reaching alarm before anything is
# allowed to STOP COLLECTING on the strength of venue evidence.
#
# Nothing consumed binance_status for decisions at first, so it was evidence,
# not a control. That becomes a real gap the moment the collection gate relies
# on it — a human-reaching alarm is a precondition of that gate.
#
# The failure it guards is quiet by construction. A degraded exchangeInfo
# response does not raise and does not empty the store; it just answers
# 'unknown' for everything, and the only trace is one INFO summary line whose
# counts nobody diffs. The evidence the collection gate depends on would evaporate between
# two cycles with no error anywhere.
#
# Fires on the LOSS, never on a gain — a venue legitimately reclassifying a
# handful of pairs must not page anyone.
_VENUE_COVERAGE_LOSS_RATIO = 0.80   # >20% of known verdicts lost in one cycle
_VENUE_COVERAGE_MIN_BEFORE = 20     # below this, noise; do not judge


def _alarm_on_venue_coverage_loss(before: dict, after: dict) -> None:
    """Page a human when this cycle lost a material share of venue evidence."""
    prior = before.get('known', 0)
    now = after.get('known', 0)
    if prior < _VENUE_COVERAGE_MIN_BEFORE or now >= prior * _VENUE_COVERAGE_LOSS_RATIO:
        return
    msg = (
        f"venue-status coverage COLLAPSED: Binance verdicts fell "
        f"from {prior} to {now} in one cycle "
        f"(trading {before.get('trading', 0)}->{after.get('trading', 0)}, "
        f"suspended {before.get('suspended', 0)}->{after.get('suspended', 0)}). "
        f"The payload passed the health floor, so this is not a fetch failure — "
        f"something changed in what Binance reports. Do NOT let a delisted-gate "
        f"act on this store until it is explained."
    )
    logger.error(msg)   # -> error tracker via the Sentry logging integration
    try:
        from carrac.notifications.ntfy import send_ntfy
        send_ntfy(
            body=msg,
            title="⚡ Carrac instrument-master alert",
            priority="4",
            tags="rotating_light",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("coverage ntfy failed (swallowed): %s", e)


def _alarm_on_delist_candidates(candidates: list) -> None:
    """Page a human when a symbol NEWLY meets the delisting conjunction.

    The rule proposes; only the curated list in
    ``instrument_master.LIFECYCLE_OVERRIDES`` disposes. So this is the whole
    of the rule's authority — it asks a person to look, and changes nothing.

    Edge-triggered by ``mark_delist_candidates``: a symbol that stays a
    candidate for weeks is announced once, not every 30 minutes. On a normal
    day this fires zero times — the four symbols Binance currently reports
    BREAK for that are NOT curated (BEAM, LIT, RDNT, XMR) all carry bars from
    today, so none of them qualifies. That is the answer to "what does this
    alarm look like on a normal day", and it is why it can be sent at high
    priority without becoming furniture.
    """
    if not candidates:
        return
    import instrument_master
    lines = "; ".join(
        f"{c['ticker']} (last bar {c['last_bar_date']}, {c['age_days']}d, "
        f"via {c['authoritative_source']})"
        for c in candidates
    )
    msg = (
        f"{len(candidates)} symbol(s) now meet the delisting "
        f"conjunction — Binance reports the pair suspended AND the stored bars "
        f"stopped more than {instrument_master.DELIST_CANDIDATE_STALE_DAYS} days "
        f"ago: {lines}. "
        f"Nothing has been changed. Collection and publishing continue until a "
        f"human adds the symbol to instrument_master.LIFECYCLE_OVERRIDES with a "
        f"reason. Check it still trades somewhere Carrac could read before "
        f"condemning it — BEAM, LIT, RDNT and XMR all look like this and are alive."
    )
    logger.error(msg)   # -> error tracker via the Sentry logging integration
    try:
        from carrac.notifications.ntfy import send_ntfy
        send_ntfy(
            body=msg,
            title="\u26a0\ufe0f Carrac delisting candidate",
            priority="4",
            tags="ghost",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("delist-candidate ntfy failed (swallowed): %s", e)


def _write_instrument_master() -> None:
    """Regenerate ``data/_instrument_master.json`` beside the CSV caches.

    The store is the reconciliation of cache FILES against the
    tracked manifest, so it is written here, immediately after
    ``_write_tracked_symbols_manifest`` — one of its two inputs. Writing them
    together is what keeps them consistent by construction; a separate daily
    job would leave a window where the manifest is fresh and the store
    disagrees with it, and the whole point of this layer is that two sets no
    longer disagree with nothing to arbitrate.

    Cheap enough to belong on this path: measured 0.40s to reconcile 376 files
    of ~2000 rows and 4ms to write the 155KB result, against a cycle that
    fetches hundreds of symbols on a ~30 minute cadence.

    ``instrument_master`` lives at the repo root and is stdlib-only at module
    scope on purpose, so the host cron can read the store without triggering
    ``carrac/__init__``'s error-tracking and analytics bootstrap. That constraint is
    one-directional: importing it from inside the app, as here, is fine.

    NOT best-effort, deliberately unlike the manifest writer above: a failure
    is logged at error AND beaconed to the error tracker. A silent failure here would
    freeze the store at its last-good content, and every consumer would keep
    reading it as current — and a warning-only except
    around a write would hide that.
    Consumers must ALSO check ``written_at`` rather than trust the file's
    existence; this alarm is the other half of that.
    """
    try:
        import os as _os

        import instrument_master

        data_dir = _os.environ.get('DATA_DIR', 'data')
        records = instrument_master.reconcile(data_dir=data_dir,
                                              existing=instrument_master.load_records(data_dir=data_dir))

        # Listing status from the venue itself.
        # One free exchangeInfo call answers "is this pair still trading?" for
        # the Binance-routed majority, instead of inferring it from bar
        # patterns — which is the inference this whole mechanism exists to remove.
        #
        # ⚠ ABSENCE IS NOT DELISTING, and that asymmetry is deliberate.
        # `fetch_binance_exchange_info` returns {} on ANY failure (network,
        # timeout, bad JSON), and `binance_status_by_ticker` maps a ticker it
        # cannot find to 'unknown'. If absence meant 'delisted', one failed
        # fetch would mark all ~188 Binance-routed symbols delisted at once —
        # and with the collection gate wired that would stop collecting nearly the whole
        # universe on a transient network blip. So this only ever records what
        # the venue positively states: TRADING -> trading, BREAK -> suspended.
        # Turning absence into a delisting needs a payload-health gate and is
        # a separate, deliberate decision.
        # Two scopes, deliberately different:
        #
        #  * `binance_tickers` — symbols Binance is the AUTHORITATIVE source
        #    for. Only these may move the instrument-level `status`.
        #  * `venue_tickers` — every crypto-routed symbol, including the ones
        #    rerouted to CCXT/Hyperliquid *because* Binance stopped
        #    serving them. Binance still reports BREAK for those pairs, and
        #    that verdict lands in the venue-scoped `binance_status` only.
        #
        # Keeping them apart is the whole point. Binance says BREAK for XMR,
        # BEAM, LIT and RDNT, all of which trade actively elsewhere with real
        # volume — writing the venue's verdict into `status` would mark live
        # assets dead. `status` stays the instrument-level answer.
        binance_tickers = [
            rec.ticker for rec in records.values()
            if instrument_master.derive_authoritative_source(rec.ticker) == 'binance'
        ]
        venue_tickers = instrument_master.venue_status_tickers(records)
        info = instrument_master.fetch_binance_exchange_info()
        # `info.get('symbols')` truthiness was the
        # only health check, and a SHORT payload passes it while erasing every
        # accumulated venue verdict (absent ticker -> 'unknown'). See
        # instrument_master.EXCHANGE_INFO_MIN_SYMBOLS.
        coverage_before = instrument_master.venue_status_coverage(records)
        if instrument_master.is_healthy_exchange_info(info):
            status_by_ticker = instrument_master.binance_status_by_ticker(
                info, binance_tickers)
            records = instrument_master.apply_status_updates(records, status_by_ticker)
            venue_status = instrument_master.binance_status_by_ticker(
                info, venue_tickers)
            records = instrument_master.apply_binance_venue_status(
                records, venue_status)
            _alarm_on_venue_coverage_loss(
                coverage_before, instrument_master.venue_status_coverage(records))
        else:
            n_symbols = len((info or {}).get('symbols') or [])
            logger.warning(
                "Binance exchangeInfo unusable (%d symbols, floor "
                "%d) — statuses left as they were for %d Binance-routed "
                "symbols (NOT marked delisted; absence is not evidence)",
                n_symbols, instrument_master.EXCHANGE_INFO_MIN_SYMBOLS,
                len(binance_tickers))

        # the curated list is the FINAL word on `status`, so
        # it is stamped after the venue ingestion, not before. Order is
        # load-bearing — `apply_status_updates` would otherwise put LRC back
        # to 'suspended' (Binance's verdict about its own pair) over the
        # 'delisted' a human recorded.
        records = instrument_master.apply_lifecycle_overrides(records)

        # ...and the rule that PROPOSES additions to that list. It writes no
        # status; it only asks a human to look. See _alarm_on_delist_candidates.
        candidates = instrument_master.delist_candidates(records)
        records, _newly_flagged = instrument_master.mark_delist_candidates(
            records, candidates)
        _alarm_on_delist_candidates(_newly_flagged)

        instrument_master.write_store(records, data_dir=data_dir)

        states = {}
        statuses = {}
        venue_statuses = {}
        for rec in records.values():
            states[rec.tracking_state] = states.get(rec.tracking_state, 0) + 1
            statuses[rec.status] = statuses.get(rec.status, 0) + 1
            venue_statuses[rec.binance_status] = venue_statuses.get(rec.binance_status, 0) + 1
        logger.info(
            "instrument master written — %d records (%s) status(%s) "
            "binance_status(%s) delist_candidates=%d (new=%d)",
            len(records),
            ", ".join(f"{k}={v}" for k, v in sorted(states.items())),
            ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())),
            ", ".join(f"{k}={v}" for k, v in sorted(venue_statuses.items())),
            len(candidates), len(_newly_flagged),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("instrument master write FAILED: %s", e, exc_info=True)
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
        except Exception:  # noqa: BLE001
            pass


def _alt_season_alarm(reason: str) -> None:
    """Human-reaching alarm when the Alt Season Index cannot be measured.

    Same shape as the stale-batch alarm above: log loudly, then beacon to
    the error tracker, and never let the alarm path take down the snapshot cycle.
    """
    msg = (
        f"Alt Season Index unavailable at the hourly sentiment snapshot: {reason}. "
        f"The reading is being withheld (NULL) rather than substituted, so the "
        f"sentiment bar, ticker, widget and MCP tool all show it as unavailable. "
        f"Check the top-coin universe and 90d performance inputs."
    )
    logger.error("ALT-SEASON ALARM: %s", msg)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(msg, level='error')
    except Exception:
        # logger.error above already fired — this is not a silent loss.
        logger.warning("alt-season alarm: Sentry capture failed", exc_info=True)


def _record_sentiment_snapshot():
    """Fetch current sentiment and store a snapshot row."""
    from carrac.db_pool import get_conn, adapt_query
    from carrac.analysis import fetch_fear_greed_index

    db = get_services().db

    try:
        sentiment = {}

        try:
            fg = fetch_fear_greed_index()
            sentiment['fear_greed'] = fg.get('value')
        except Exception:
            sentiment['fear_greed'] = None

        try:
            fr = db.get_funding_rate('BTC')
            sentiment['funding_rate'] = fr
        except Exception:
            sentiment['funding_rate'] = None

        try:
            oi = db.fetch_open_interest('BTC')
            sentiment['open_interest_usd'] = oi.get('value_usd')
            sentiment['oi_change_pct'] = oi.get('change_pct')
        except Exception:
            sentiment['open_interest_usd'] = None
            sentiment['oi_change_pct'] = None

        try:
            mvrv = db.fetch_mvrv_zscore()
            sentiment['mvrv_zscore'] = mvrv.get('zscore')
        except Exception:
            sentiment['mvrv_zscore'] = None

        try:
            alt = db.calculate_alt_season_index()
            sentiment['alt_season'] = alt.get('index')
            if sentiment['alt_season'] is None:
                # The reading now correctly degrades to NULL instead
                # of a fabricated 50 — but a silent NULL is the same "decision not
                # to know" that would hide the problem. This snapshot runs on a fixed
                # cadence, so it is the one rate-bounded place an alarm belongs
                # (the five request-path surfaces would spam the error tracker).
                _alt_season_alarm(alt.get('error') or 'unknown reason')
        except Exception as e:
            sentiment['alt_season'] = None
            _alt_season_alarm(f'calculate_alt_season_index raised: {e}')

        try:
            ls = db.fetch_long_short_ratio(['BTC'])
            btc_ls = ls.get('BTC', {})
            sentiment['btc_long_pct'] = btc_ls.get('longAccount') or btc_ls.get('longPct')
        except Exception:
            sentiment['btc_long_pct'] = None

        with get_conn() as conn:
            conn.execute(
                adapt_query('''INSERT INTO sentiment_history
                   (timestamp, fear_greed, funding_rate, open_interest_usd, oi_change_pct, mvrv_zscore, alt_season, btc_long_pct)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)'''),
                (datetime.now().isoformat(), sentiment['fear_greed'], sentiment['funding_rate'],
                 sentiment['open_interest_usd'], sentiment['oi_change_pct'],
                 sentiment['mvrv_zscore'], sentiment['alt_season'], sentiment['btc_long_pct'])
            )

            # The regime row is best-effort, but on PostgreSQL a failed
            # statement aborts the whole transaction.  The savepoint confines
            # the failure so the sentiment row above still commits.
            regime_recorded = False
            try:
                from carrac.sentiment.regime import gather_regime_inputs, compute_market_regime
                regime = compute_market_regime(**gather_regime_inputs())
                conn.execute('SAVEPOINT regime_snapshot')
                try:
                    conn.execute(
                        adapt_query('INSERT INTO regime_history (timestamp, regime, score, components) VALUES (?,?,?,?)'),
                        (datetime.now().isoformat(), regime['regime'], regime['score'],
                         json.dumps(regime['components']))
                    )
                except Exception:
                    conn.execute('ROLLBACK TO SAVEPOINT regime_snapshot')
                    raise
                conn.execute('RELEASE SAVEPOINT regime_snapshot')
                regime_recorded = True
            except Exception as exc:
                logger.error("regime_history: snapshot not recorded: %s", exc, exc_info=True)
        if regime_recorded:
            logger.info("Recorded sentiment snapshot")
        else:
            logger.info("Recorded sentiment snapshot (without regime row)")
    except Exception as e:
        logger.error("sentiment_history: snapshot not recorded: %s", e)


def _start_sentiment_scheduler():
    """Start background thread that records sentiment every hour."""
    def loop():
        _stop_sentiment.wait(random.randint(0, 120))
        while not _stop_sentiment.is_set():
            _record_sentiment_snapshot()
            _stop_sentiment.wait(3600)

    t = threading.Thread(target=loop, daemon=True, name="SentimentScheduler")
    t.start()
    logger.info("Sentiment history scheduler started (hourly)")
