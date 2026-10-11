"""
Server-side formula evaluator — mirrors static/js/formula-parser.js in Python.

Evaluates formula expressions like "RSI(14) < 30 AND CROSSOVER(EMA(9), EMA(21))"
against OHLCV data for a given symbol/timeframe. Used by:
- Play conditions (formula_expression type in conditions.py)
- Formula alerts (check_formula_alerts in indicator_state.py)

Architecture: tokenize → parse → evaluate (same as JS version).
Data: OHLCV from CSV cache + quant series from Quant DB (on-demand).
Safety: whitelist-only functions, no eval/exec, expression length + call limits.
"""

import functools
import logging
import math
import re
import time
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger('carrac.formula_eval')

# Engine version — bump when semantics change so saved formulas can be migrated.
# v3: adds HTF, DIVERGENCE, per-asset derivatives vars, _dates context (0.83.9).
ENGINE_VERSION = 3

MAX_EXPRESSION_LENGTH = 500
MAX_FUNCTION_CALLS = 50
EVAL_TIMEOUT_MS = 200
OHLCV_BARS = 500

# ── Quant Variables (mirrors JS FormulaParser.QUANT_VARS) ──────────────

QUANT_VARS = {
    'm2': 'M2', 'net_liquidity': 'NET_LIQUIDITY', 'dxy': 'DXY',
    'stablecoin': 'STABLECOIN_USDT_SUPPLY',
    'fear_greed': 'FEAR_GREED', 'alt_season': 'ALT_SEASON',
    'etf_flow': 'ETF_FLOW',
    'vix': 'VIX', 'hy_spread': 'HY_SPREAD', 'yield_curve': 'YIELD_CURVE',
    'regime': 'REGIME',
    'btc': 'BTC', 'eth': 'ETH', 'sol': 'SOL', 'spx': 'SPX', 'gold': 'GOLD_F',
    'risk_appetite': 'RISK_APPETITE', 'liquidity_factor': 'LIQUIDITY_FACTOR',
    'credit_stress_factor': 'CREDIT_STRESS_FACTOR',
    # Transmission chain strengths (scalar 0-1, written nightly by leadlag engine)
    'chain_monetary_policy': 'CHAIN_MONETARY_POLICY',
    'chain_dollar_liquidity': 'CHAIN_DOLLAR_LIQUIDITY',
    'chain_risk_appetite': 'CHAIN_RISK_APPETITE',
    # 'funding' and 'dvol' moved to DERIV_VARS — they are
    # quant.derivatives columns and now resolve to the formula's own symbol
    # instead of silently serving BTC's series on every chart.
    # 'scanner'/'consensio'/'ribbon'/'hurst' are deliberately absent: their
    # nothing writes them into quant.signals, so such vars would never resolve
    # to data and every formula using them would fail with "Unknown
    # identifier". Add them only together with a scheduled writer and
    # backfilled history.
}

# Per-asset derivatives vars — resolve against quant.derivatives for the
# formula's current symbol (BTC → BTC's skew, ETH → ETH's). Distinct from
# QUANT_VARS which load fixed globally-scoped series.
#
# Value = DB column name in quant.derivatives.
DERIV_VARS = {
    'basis_apr':        'basis_pct',
    'skew_25d':         'skew_25d',
    'iv_7d':            'iv_7d',
    'iv_30d':           'iv_30d',
    'iv_90d':           'iv_90d',
    'term_slope':       'term_structure_slope',
    'gex_net':          'gex_net',
    'gex_flip_price':   'gex_flip_price',
    'iv_rv_premium':    'iv_rv_premium',
    # 'funding_oi_ratio' is deliberately absent — it would map to a column that
    # does not exist (no collector writes it; quant.derivatives has only
    # funding_rate_avg / funding_rate_cross_avg), so every formula using it
    # would error claiming a per-asset coverage gap. If the concept is wanted, the meaningful
    # combination is funding x OI x intervals/day (what the long side pays per
    # day) — morning_engine/data_assembly.py computes it as `funding_flow`.
    # These resolve per symbol, not as global BTC series.
    # funding_rate_avg has rows for 10 assets; dvol for BTC/ETH only —
    # symbols without rows fail loudly instead of silently getting BTC's.
    'funding':          'funding_rate_avg',
    'dvol':             'dvol',
}

# symbol-resolution scope per DSL variable, exposed through
# indicator_get_dsl_reference so automated callers above all can tell whether a
# var tracks the chart's symbol or a single shared series.
#   per_symbol    — resolves against the formula's own symbol
#   global        — one market-wide series regardless of symbol
#   btc_reference — a single BTC-anchored series (name does not say so);
#                   using it on another symbol adds a warning to the result
VAR_SCOPE = {
    **{name: 'per_symbol' for name in DERIV_VARS},
    **{name: 'global' for name in QUANT_VARS},
    'etf_flow': 'btc_reference',   # quant.onchain ETF_BTC_NET_FLOW
}

# ── OHLCV Provider ─────────────────────────────────────────────────────

_csv_cache = None

def _get_csv_cache():
    global _csv_cache
    if _csv_cache is None:
        from csv_cache import CSVCache
        _csv_cache = CSVCache()
    return _csv_cache


# Timeframe ordering for HTF validation: lower index = shorter TF.
# HTF(target, ...) is only valid when target index >= caller index (never lower).
TF_ORDER = {'1h': 0, '2h': 1, '4h': 2, '1D': 3, '3D': 4, '1W': 5, '1M': 6}

# Canonical timeframe -> (source CSV timeframe, pandas resample
# rule or None). This map is the ONLY place a timeframe becomes bars.
#
# It replaces an `if tf in ('1h','2h','4h'): ... else: csv_tf = 'daily'` chain
# whose else-branch silently served DAILY bars for any unrecognised string —
# so a 15m backtest returned daily results, with no error, indistinguishable
# from a real one. Unknown timeframes now return None (loud) instead.
#
# 2h/4h resample from the 1h CSVs. Both were already advertised in the MCP
# tool schemas and in TF_ORDER, but mapped to `{SYMBOL}_2h.csv` /
# `{SYMBOL}_4h.csv` files that are not written by any collector — so every
# 4h call failed with "No OHLCV data" until now.
_TF_SOURCE = {
    '1h': ('1h', None),
    '2h': ('1h', '2h'),
    '4h': ('1h', '4h'),
    '1D': ('daily', None),
    '3D': ('daily', '3D'),
    '1W': ('daily', 'W'),
    '1M': ('daily', 'MS'),
}

# Case-forgiving aliases for the unambiguous timeframes only.
# ⚠ '1m' is deliberately NOT aliased to '1M': lowercase means one MINUTE,
# uppercase means one MONTH. Folding them together would serve monthly bars
# to a minute request — the exact silent-wrong-answer class this map exists
# to kill. Carrac stores no sub-hourly data, so '1m'/'5m'/'15m' stay unsupported.
_TF_ALIASES = {'1d': '1D', '3d': '3D', '1w': '1W', '1H': '1h', '2H': '2h', '4H': '4h'}

SUPPORTED_TIMEFRAMES = tuple(_TF_SOURCE)


def canonical_timeframe(timeframe: str) -> Optional[str]:
    """Return the canonical form of `timeframe`, or None if unsupported.

    Callers should treat None as "refuse", never as "fall back to daily".
    """
    if not isinstance(timeframe, str):
        return None
    tf = timeframe.strip()
    tf = _TF_ALIASES.get(tf, tf)
    return tf if tf in _TF_SOURCE else None


# _load_ohlcv_cached now takes an explicit `requested_bars` param
# (defaulting to OHLCV_BARS) instead of hardcoding the trim depth, so a caller
# like backtest_indicator_formula can ask for more history than the hot
# formula-eval path (/api/formula/*, HTF alignment) ever needs. Bumped
# maxsize 50 -> 128: entries can now be up to ~6x bigger (2000+ bars vs 500),
# but a deep-history backtest call creates a NEW cache key (bars is part of
# the key), so without headroom it could evict the hot 500-bar entries that
# /api/formula/* and HTF alignment rely on. Worst case 128 entries * ~6
# arrays * ~2200 bars * 8 bytes ≈ 13MB — trivial next to the correctness win.
@functools.lru_cache(maxsize=128)
def _load_ohlcv_cached(symbol: str, timeframe: str, cache_key: int,
                        requested_bars: int = OHLCV_BARS) -> Optional[dict]:
    """Load OHLCV from CSV. cache_key = time() // 300 for 5-min TTL.

    `requested_bars` is part of the cache key (functools.lru_cache hashes all
    bound args) so a 2000-bar request can never be served a stale 500-bar
    result, or vice versa.

    Returns dict with OHLCV arrays plus a `_dates` numpy datetime64 array
    (UTC day/hour resolution) for cross-TF alignment (HTF operator), and
    `_total_available` — the TRUE number of bars this symbol/timeframe has on
    disk (post-resample, pre-trim), regardless of how many were requested.
    """
    cache = _get_csv_cache()

    # Resolve via _TF_SOURCE. An unsupported timeframe returns
    # None so the caller surfaces "no data" — it must never quietly become a
    # daily result the caller cannot distinguish from a real one.
    canon = canonical_timeframe(timeframe)
    if canon is None:
        logger.warning(
            "Unsupported timeframe %r for %s — refusing to fall back to daily. "
            "Supported: %s", timeframe, symbol, ', '.join(SUPPORTED_TIMEFRAMES)
        )
        return None

    csv_tf, resample_rule = _TF_SOURCE[canon]

    df = cache.load(symbol, csv_tf)
    if df is None or df.empty:
        return None

    if resample_rule:
        df = df.resample(resample_rule).agg({
            'open': 'first', 'high': 'max', 'low': 'min',
            'close': 'last', 'volume': 'sum'
        }).dropna(subset=['close'])

    total_available = len(df)

    df = df.tail(max(1, int(requested_bars)))
    if df.empty:
        return None

    # Preserve DatetimeIndex as `_dates` (numpy datetime64[ns]) — needed by HTF alignment.
    return {
        'open': df['open'].values.astype(np.float64),
        'high': df['high'].values.astype(np.float64),
        'low': df['low'].values.astype(np.float64),
        'close': df['close'].values.astype(np.float64),
        'volume': df['volume'].values.astype(np.float64),
        '_dates': df.index.values.astype('datetime64[ns]'),
        '_total_available': total_available,
    }


def load_ohlcv(symbol: str, timeframe: str = '1D',
                requested_bars: Optional[int] = None) -> Optional[dict]:
    """Load OHLCV for a symbol/timeframe.

    `requested_bars` defaults to OHLCV_BARS (500) so every existing caller
    (the /api/formula/* hot path, HTF alignment, evaluate_formula) is
    byte-for-byte unchanged. Only backtest_indicator_formula asks for more.
    """
    cache_key = int(time.time()) // 300
    bars = OHLCV_BARS if requested_bars is None else int(requested_bars)
    return _load_ohlcv_cached(symbol.upper(), timeframe, cache_key, bars)


# ── Quant Provider ─────────────────────────────────────────────────────

@functools.lru_cache(maxsize=30)
def _load_quant_series_cached(series_name: str, num_bars: int, cache_key: int) -> Optional[list]:
    """Load a quant series from the DB. Returns list of (date, float)."""
    try:
        from quant.bridge import _fetch_series
        end_date = date.today()
        start_date = end_date - timedelta(days=num_bars * 2)  # buffer for sparse data
        return _fetch_series(series_name, start_date=start_date, end_date=end_date)
    except Exception as e:
        logger.debug(f"Quant series {series_name} not available: {e}")
        return None


def _load_quant_into_context(context: dict, quant_deps: list, num_bars: int):
    """Load quant series and merge into OHLCV context as aligned arrays."""
    if not quant_deps:
        return

    cache_key = int(time.time()) // 300

    # We need dates from the OHLCV context to align quant data.
    # Since we don't have timestamps, use simple forward-fill by index position.
    for var_name, series_name in quant_deps:
        raw = _load_quant_series_cached(series_name, num_bars, cache_key)
        if not raw:
            continue

        # Simple alignment: take last num_bars values, forward-fill to array length
        values = [v for _, v in raw]
        arr = np.full(num_bars, np.nan)
        if values:
            # Place values at the end of the array (most recent)
            src_len = min(len(values), num_bars)
            arr[-src_len:] = values[-src_len:]
            # Forward-fill NaN gaps
            last_valid = np.nan
            for i in range(num_bars):
                if not np.isnan(arr[i]):
                    last_valid = arr[i]
                elif not np.isnan(last_valid):
                    arr[i] = last_valid
        context[var_name] = arr


# Deribit-derived variables are admin-only. Filtering at the
# dependency SCAN (rather than only at fetch time) means a non-admin formula
# referencing `dvol` reports an unknown variable, instead of quietly
# evaluating against a series that returns nothing — a silently different
# answer is worse than a refusal. quant/bridge.py blocks the data itself as
# well, so neither layer is load-bearing alone.
def _visible_quant_vars() -> dict:
    from carrac.compliance.deribit_gate import allowed_formula_vars
    return allowed_formula_vars(QUANT_VARS)


def _visible_deriv_vars() -> dict:
    from carrac.compliance.deribit_gate import allowed_formula_vars
    return allowed_formula_vars(DERIV_VARS)


def get_quant_deps(expression: str) -> list:
    """Scan expression for quant variable references.
    Returns list of (var_name, series_name) tuples."""
    deps = []
    seen = set()
    # Simple token scan — find identifiers that match QUANT_VARS keys
    for m in re.finditer(r'[a-zA-Z_]\w*', expression.lower()):
        name = m.group()
        if name in _visible_quant_vars() and name not in seen:
            # Don't flag if it's used as a function call (followed by open paren)
            after = expression[m.end():m.end() + 1]
            if after != '(':
                seen.add(name)
                deps.append((name, QUANT_VARS[name]))
    return deps


def get_deriv_deps(expression: str) -> list:
    """Scan expression for per-asset derivatives variable references.
    Returns list of (var_name, db_column) tuples."""
    deps = []
    seen = set()
    for m in re.finditer(r'[a-zA-Z_]\w*', expression.lower()):
        name = m.group()
        if name in _visible_deriv_vars() and name not in seen:
            after = expression[m.end():m.end() + 1]
            if after != '(':
                seen.add(name)
                deps.append((name, DERIV_VARS[name]))
    return deps


@functools.lru_cache(maxsize=60)
def _load_deriv_series_cached(asset: str, col: str, num_bars: int, cache_key: int) -> Optional[list]:
    """Load one per-asset column from quant.derivatives. Returns list of (date, value).

    Safe: column name is whitelisted (lives in DERIV_VARS values) and validated against
    a strict pattern, so no SQL injection via format-string.
    """
    # Defense in depth — col must be a known derivatives column.
    if col not in DERIV_VARS.values():
        return None
    if not re.match(r'^[a-z_][a-z0-9_]*$', col):
        return None
    try:
        from carrac.db_pool import get_quant_conn as get_conn
        end_date = date.today()
        start_date = end_date - timedelta(days=num_bars * 2)  # buffer for gaps
        sql = (
            f"SELECT ts::date AS d, {col} AS value "
            f"FROM quant.derivatives "
            f"WHERE asset = %s AND {col} IS NOT NULL "
            f"  AND ts::date >= %s AND ts::date <= %s "
            f"ORDER BY ts ASC"
        )
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, (asset.upper(), start_date, end_date))
            rows = cur.fetchall()
            # Deduplicate by day — keep last value per day (derivatives are 6h cadence)
            by_day = {}
            for row in rows:
                v = row['value']
                if v is None:
                    continue
                by_day[row['d']] = float(v)
            return sorted(by_day.items())
    except Exception as e:
        logger.debug(f"Derivatives series {asset}/{col} not available: {e}")
        return None


def _load_deriv_into_context(context: dict, deriv_deps: list, asset: str, num_bars: int):
    """Load per-asset derivatives series and merge into context as aligned arrays.

    A per-symbol var with no rows for `asset` raises ValueError
    naming the var and the symbol. Skipping the var would leave the identifier
    out of the context, so evaluation would fail with "Unknown identifier
    'IV_30D'" — loud but misleading for a documented variable. It must never fall back to
    another asset's series: silent substitution is how `FUNDING < 0` on an
    ETH chart quietly backtested BTC funding.
    """
    if not deriv_deps or not asset:
        return

    cache_key = int(time.time()) // 300

    for var_name, col in deriv_deps:
        raw = _load_deriv_series_cached(asset.upper(), col, num_bars, cache_key)
        if not raw:
            raise ValueError(
                f"{var_name.upper()} has no data for {asset.upper()} — it is a "
                f"per-symbol derivatives series and resolves to the formula's own "
                f"symbol. Coverage: funding for the major perp assets, options-derived "
                f"series (iv_*, skew, gex, dvol) for BTC/ETH only."
            )

        # Align by date if possible — use _dates in context.
        arr = np.full(num_bars, np.nan)
        if '_dates' in context and len(context['_dates']) == num_bars:
            # Date-aware forward-fill
            by_day = dict(raw)
            last_val = np.nan
            # Iterate chart bars in order, carrying forward the most recent value whose
            # date is <= the bar date.
            ordered = sorted(by_day.items())
            oi = 0
            for i in range(num_bars):
                bar_date = context['_dates'][i].astype('datetime64[D]').astype(object)
                while oi < len(ordered) and ordered[oi][0] <= bar_date:
                    last_val = ordered[oi][1]
                    oi += 1
                arr[i] = last_val
        else:
            # Fallback: place values at the tail + forward-fill
            values = [v for _, v in raw]
            if values:
                src_len = min(len(values), num_bars)
                arr[-src_len:] = values[-src_len:]
                last_valid = np.nan
                for i in range(num_bars):
                    if not np.isnan(arr[i]):
                        last_valid = arr[i]
                    elif not np.isnan(last_valid):
                        arr[i] = last_valid
        context[var_name] = arr


# ── TA Functions (numpy) ───────────────────────────────────────────────

def _nan(n):
    return np.full(n, np.nan)


def _ast_contains_call(node, fn_name: str) -> bool:
    """Return True if the AST `node` (or any descendant) is a call to `fn_name`.

    Used by HTF to reject nested HTF calls up-front, before any data load
    that could short-circuit the recursive evaluator path.
    """
    if not isinstance(node, dict):
        return False
    if node.get('type') == 'call' and node.get('name', '').lower() == fn_name.lower():
        return True
    for v in node.values():
        if isinstance(v, dict) and _ast_contains_call(v, fn_name):
            return True
        if isinstance(v, list):
            for item in v:
                if _ast_contains_call(item, fn_name):
                    return True
    return False


def _find_seed(src, period):
    """Return index of the last bar in the first consecutive `period`-sized
    non-NaN window, or -1 if no such window exists.

    Mirrors the JS _findSeed() in static/js/ta-lib.js — needed so EMA/Wilders
    can seed correctly when `src` has a NaN prefix (e.g. MACD line).
    """
    p = int(period)
    if p < 1:
        return -1
    consecutive = 0
    for i in range(len(src)):
        if np.isnan(src[i]):
            consecutive = 0
            continue
        consecutive += 1
        if consecutive == p:
            return i
    return -1


def _sma(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    cs = np.nancumsum(src)
    cs = np.insert(cs, 0, 0.0)
    for i in range(p - 1, n):
        window = src[i - p + 1:i + 1]
        if np.any(np.isnan(window)):
            continue
        out[i] = (cs[i + 1] - cs[i - p + 1]) / p
    return out


def _ema(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    k = 2.0 / (p + 1)
    # Find first p-sized valid window (skips NaN prefix from derived inputs)
    seed_end = _find_seed(src, p)
    if seed_end < 0:
        return out
    out[seed_end] = float(np.mean(src[seed_end - p + 1:seed_end + 1]))
    for i in range(seed_end + 1, n):
        if np.isnan(src[i]) or np.isnan(out[i - 1]):
            continue
        out[i] = src[i] * k + out[i - 1] * (1 - k)
    return out


def _wilders(src, period):
    """Wilder's smoothing (RMA)."""
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    seed_end = _find_seed(src, p)
    if seed_end < 0:
        return out
    out[seed_end] = float(np.mean(src[seed_end - p + 1:seed_end + 1]))
    for i in range(seed_end + 1, n):
        if np.isnan(src[i]) or np.isnan(out[i - 1]):
            continue
        out[i] = (out[i - 1] * (p - 1) + src[i]) / p
    return out


def _rsi(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p + 1:
        return out
    deltas = np.diff(src)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:p])
    avg_loss = np.mean(losses[:p])
    if avg_loss == 0:
        out[p] = 100.0
    else:
        out[p] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(p, n - 1):
        d = src[i + 1] - src[i]
        g = d if d > 0 else 0.0
        lo = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (p - 1) + g) / p
        avg_loss = (avg_loss * (p - 1) + lo) / p
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            out[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _tr(h, l, c):
    n = len(h)
    out = _nan(n)
    out[0] = h[0] - l[0]
    for i in range(1, n):
        out[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return out


def _atr(h, l, c, period):
    return _wilders(_tr(h, l, c), period)


def _stdev(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    for i in range(p - 1, n):
        window = src[i - p + 1:i + 1]
        if np.any(np.isnan(window)):
            continue
        out[i] = np.std(window, ddof=0)
    return out


def _highest(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    for i in range(p - 1, n):
        window = src[i - p + 1:i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) > 0:
            out[i] = np.max(valid)
    return out


def _lowest(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    for i in range(p - 1, n):
        window = src[i - p + 1:i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) > 0:
            out[i] = np.min(valid)
    return out


def _bb_upper(src, period, mult):
    mid = _sma(src, period)
    sd = _stdev(src, period)
    n = len(src)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(mid[i]) and not np.isnan(sd[i]):
            out[i] = mid[i] + sd[i] * mult
    return out


def _bb_lower(src, period, mult):
    mid = _sma(src, period)
    sd = _stdev(src, period)
    n = len(src)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(mid[i]) and not np.isnan(sd[i]):
            out[i] = mid[i] - sd[i] * mult
    return out


def _bb_mid(src, period):
    return _sma(src, period)


def _keltner_upper(h, l, c, period, mult):
    mid = _ema(c, period)
    a = _atr(h, l, c, period)
    n = len(h)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(mid[i]) and not np.isnan(a[i]):
            out[i] = mid[i] + a[i] * mult
    return out


def _keltner_lower(h, l, c, period, mult):
    mid = _ema(c, period)
    a = _atr(h, l, c, period)
    n = len(h)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(mid[i]) and not np.isnan(a[i]):
            out[i] = mid[i] - a[i] * mult
    return out


def _wma(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    denom = p * (p + 1) / 2
    for i in range(p - 1, n):
        s = 0.0
        bad = False
        for j in range(p):
            v = src[i - p + 1 + j]
            if np.isnan(v):
                bad = True
                break
            s += v * (j + 1)
        if not bad:
            out[i] = s / denom
    return out


def _hma(src, period):
    p = int(period)
    half = max(1, p // 2)
    sqrt_p = max(1, int(math.sqrt(p)))
    wma_half = _wma(src, half)
    wma_full = _wma(src, p)
    n = len(src)
    diff = _nan(n)
    for i in range(n):
        if not np.isnan(wma_half[i]) and not np.isnan(wma_full[i]):
            diff[i] = 2 * wma_half[i] - wma_full[i]
    return _wma(diff, sqrt_p)


def _crossover(a, b):
    n = len(a)
    out = _nan(n)
    out[0] = 0.0
    for i in range(1, n):
        if np.isnan(a[i]) or np.isnan(b[i]) or np.isnan(a[i-1]) or np.isnan(b[i-1]):
            out[i] = 0.0
        else:
            out[i] = 1.0 if (a[i] > b[i] and a[i-1] <= b[i-1]) else 0.0
    return out


def _crossunder(a, b):
    n = len(a)
    out = _nan(n)
    out[0] = 0.0
    for i in range(1, n):
        if np.isnan(a[i]) or np.isnan(b[i]) or np.isnan(a[i-1]) or np.isnan(b[i-1]):
            out[i] = 0.0
        else:
            out[i] = 1.0 if (a[i] < b[i] and a[i-1] >= b[i-1]) else 0.0
    return out


def _offset(src, n_offset):
    n_off = int(n_offset)
    n = len(src)
    out = _nan(n)
    for i in range(n):
        j = i - n_off
        if 0 <= j < n:
            out[i] = src[j]
    return out


def _change(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    for i in range(p, n):
        if not np.isnan(src[i]) and not np.isnan(src[i - p]):
            out[i] = src[i] - src[i - p]
    return out


def _roc(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    for i in range(p, n):
        if not np.isnan(src[i]) and not np.isnan(src[i - p]) and src[i - p] != 0:
            out[i] = ((src[i] - src[i - p]) / src[i - p]) * 100
    return out


def _percentile(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    for i in range(p - 1, n):
        window = src[i - p + 1:i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) < 2:
            continue
        val = src[i]
        if np.isnan(val):
            continue
        out[i] = np.sum(valid < val) / len(valid) * 100
    return out


def _zscore(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 2:
        return out
    for i in range(p - 1, n):
        window = src[i - p + 1:i + 1]
        if np.any(np.isnan(window)):
            continue
        mean = np.mean(window)
        sd = np.std(window, ddof=0)
        if sd == 0:
            out[i] = 0.0
        else:
            out[i] = (src[i] - mean) / sd
    return out


def _sum(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    s = 0.0
    for i in range(p):
        s += src[i] if not np.isnan(src[i]) else 0.0
    out[p - 1] = s
    for i in range(p, n):
        leaving = src[i - p] if not np.isnan(src[i - p]) else 0.0
        entering = src[i] if not np.isnan(src[i]) else 0.0
        s += entering - leaving
        out[i] = s
    return out


def _barssince(cond):
    n = len(cond)
    out = _nan(n)
    cnt = np.nan
    for i in range(n):
        if not np.isnan(cond[i]) and cond[i] != 0:
            cnt = 0.0
        elif not np.isnan(cnt):
            cnt += 1
        out[i] = cnt
    return out


def _count(cond, period):
    p = int(period)
    n = len(cond)
    out = _nan(n)
    if n < p or p < 1:
        return out
    c = 0
    for i in range(p):
        if not np.isnan(cond[i]) and cond[i] != 0:
            c += 1
    out[p - 1] = float(c)
    for i in range(p, n):
        entering = cond[i]
        leaving = cond[i - p]
        if not np.isnan(entering) and entering != 0:
            c += 1
        if not np.isnan(leaving) and leaving != 0:
            c -= 1
        out[i] = float(c)
    return out


def _valuewhen(cond, src):
    n = len(cond)
    out = _nan(n)
    last_val = np.nan
    for i in range(n):
        if not np.isnan(cond[i]) and cond[i] != 0:
            last_val = src[i]
        out[i] = last_val
    return out


def _cum(src):
    n = len(src)
    out = _nan(n)
    total = 0.0
    for i in range(n):
        if not np.isnan(src[i]):
            total += src[i]
            out[i] = total
    return out


def _streak(cond):
    n = len(cond)
    out = np.zeros(n)
    for i in range(n):
        if not np.isnan(cond[i]) and cond[i] != 0:
            out[i] = (out[i - 1] + 1) if i > 0 else 1.0
        else:
            out[i] = 0.0
    return out


def _highest_bars(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    for i in range(p - 1, n):
        window = src[i - p + 1:i + 1]
        if np.any(np.isnan(window)):
            continue
        idx = int(np.argmax(window))
        out[i] = float(p - 1 - idx)
    return out


def _lowest_bars(src, period):
    p = int(period)
    n = len(src)
    out = _nan(n)
    if n < p or p < 1:
        return out
    for i in range(p - 1, n):
        window = src[i - p + 1:i + 1]
        if np.any(np.isnan(window)):
            continue
        idx = int(np.argmin(window))
        out[i] = float(p - 1 - idx)
    return out


def _pivothigh(src, left, right):
    lb = int(left)
    rb = int(right)
    n = len(src)
    out = _nan(n)
    if lb < 1 or rb < 1:
        return out
    for i in range(lb + rb, n):
        pivot_idx = i - rb
        pivot_val = src[pivot_idx]
        if np.isnan(pivot_val):
            continue
        is_pivot = True
        for j in range(pivot_idx - lb, pivot_idx):
            if np.isnan(src[j]) or src[j] >= pivot_val:
                is_pivot = False
                break
        if is_pivot:
            for j in range(pivot_idx + 1, pivot_idx + rb + 1):
                if j >= n or np.isnan(src[j]) or src[j] >= pivot_val:
                    is_pivot = False
                    break
        if is_pivot:
            out[i] = pivot_val
    return out


def _pivotlow(src, left, right):
    lb = int(left)
    rb = int(right)
    n = len(src)
    out = _nan(n)
    if lb < 1 or rb < 1:
        return out
    for i in range(lb + rb, n):
        pivot_idx = i - rb
        pivot_val = src[pivot_idx]
        if np.isnan(pivot_val):
            continue
        is_pivot = True
        for j in range(pivot_idx - lb, pivot_idx):
            if np.isnan(src[j]) or src[j] <= pivot_val:
                is_pivot = False
                break
        if is_pivot:
            for j in range(pivot_idx + 1, pivot_idx + rb + 1):
                if j >= n or np.isnan(src[j]) or src[j] <= pivot_val:
                    is_pivot = False
                    break
        if is_pivot:
            out[i] = pivot_val
    return out


def _corr(a, b, period):
    p = int(period)
    n = len(a)
    out = _nan(n)
    if n < p or p < 2:
        return out
    for i in range(p - 1, n):
        wa = a[i - p + 1:i + 1]
        wb = b[i - p + 1:i + 1]
        if np.any(np.isnan(wa)) or np.any(np.isnan(wb)):
            continue
        ma, mb = np.mean(wa), np.mean(wb)
        sa, sb = np.std(wa, ddof=0), np.std(wb, ddof=0)
        if sa == 0 or sb == 0:
            out[i] = 0.0
        else:
            out[i] = np.mean((wa - ma) * (wb - mb)) / (sa * sb)
    return out


def _beta(a, b, period):
    p = int(period)
    n = len(a)
    out = _nan(n)
    if n < p or p < 2:
        return out
    for i in range(p - 1, n):
        wa = a[i - p + 1:i + 1]
        wb = b[i - p + 1:i + 1]
        if np.any(np.isnan(wa)) or np.any(np.isnan(wb)):
            continue
        var_b = np.var(wb, ddof=0)
        if var_b == 0:
            out[i] = 0.0
        else:
            cov = np.mean((wa - np.mean(wa)) * (wb - np.mean(wb)))
            out[i] = cov / var_b
    return out


# ── Ichimoku ───────────────────────────────────────────────────────────

def _tenkan(h, l, period=9):
    p = int(period)
    hh = _highest(h, p)
    ll = _lowest(l, p)
    n = len(h)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(hh[i]) and not np.isnan(ll[i]):
            out[i] = (hh[i] + ll[i]) / 2
    return out


def _kijun(h, l, period=26):
    return _tenkan(h, l, period)


def _senkou_a(h, l):
    t = _tenkan(h, l, 9)
    k = _tenkan(h, l, 26)
    n = len(h)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(t[i]) and not np.isnan(k[i]):
            out[i] = (t[i] + k[i]) / 2
    return out


def _senkou_b(h, l, period=52):
    return _tenkan(h, l, period)


def _chikou(c):
    return _offset(c, -26)


# ── Alligator ──────────────────────────────────────────────────────────

def _hl2(h, l):
    return (h + l) / 2


def _alligator_jaw(h, l, period=13, shift=8):
    mid = _hl2(h, l)
    return _offset(_wilders(mid, period), shift)


def _alligator_teeth(h, l, period=8, shift=5):
    mid = _hl2(h, l)
    return _offset(_wilders(mid, period), shift)


def _alligator_lips(h, l, period=5, shift=3):
    mid = _hl2(h, l)
    return _offset(_wilders(mid, period), shift)


# ── Stoch RSI ──────────────────────────────────────────────────────────

def _stoch_rsi_k(c, period=14, k_smooth=3):
    p = int(period)
    ks = int(k_smooth)
    n = len(c)
    rsi_arr = _rsi(c, p)
    raw = _nan(n)
    for i in range(p, n):
        if np.isnan(rsi_arr[i]):
            continue
        window = rsi_arr[max(0, i - p + 1):i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) < p:
            continue
        hh, ll = np.max(valid), np.min(valid)
        rng = hh - ll
        raw[i] = 50.0 if rng == 0 else ((rsi_arr[i] - ll) / rng) * 100
    return raw if ks <= 1 else _sma(raw, ks)


def _stoch_rsi_d(c, period=14, k_smooth=3, d_smooth=3):
    return _sma(_stoch_rsi_k(c, period, k_smooth), int(d_smooth))


# ── Donchian ───────────────────────────────────────────────────────────

def _donchian_high(h, period=20):
    return _highest(h, period)

def _donchian_low(l, period=20):
    return _lowest(l, period)


# ── Detection ──────────────────────────────────────────────────────────

def _nr7(h, l):
    n = len(h)
    out = _nan(n)
    if n < 7:
        return out
    ranges = h - l
    for i in range(6, n):
        cur = ranges[i]
        is_min = True
        for j in range(1, 7):
            if ranges[i - j] <= cur:
                is_min = False
                break
        out[i] = 1.0 if is_min else 0.0
    return out


# ── SMC pattern primitives ────────────────────────────────
# Canonical names describe the price action; the SMC/ICT vocabulary is
# registered as aliases onto these same callables further down.


def _gap_up(h, l, c, atr_mult):
    """Bullish 3-bar imbalance (SMC: fair value gap up).

    Fires on the bar whose low sits entirely above the high two bars back,
    leaving an untraded band between them.

    `atr_mult` is the minimum gap size as a fraction of ATR(14), matching the
    FVG chart indicator's `atrThresholdPct` (calculateFVG in
    static/js/carrac-indicator-pack.js). Default 0.25 so GAP_UP() and the
    chart agree — Carrac already had two disagreeing definitions of "fair
    value gap" (the ATR-filtered indicator and the unfiltered SMC module,
 #3) and a third would be worse. Pass 0 for the raw gap.

    Note this uses the CURRENT bar's ATR, matching the chart indicator. That
    differs from DISPLACEMENT deliberately: a gap's size is not the bar's own
    range, so there is no self-inflation to avoid here.
    """
    m = float(atr_mult)
    n = len(h)
    out = _nan(n)
    if n < 3:
        return out
    atr = _atr(h, l, c, 14) if m > 0 else None
    for i in range(2, n):
        if np.isnan(l[i]) or np.isnan(h[i - 2]):
            continue
        gap = l[i] - h[i - 2]
        if gap <= 0:
            out[i] = 0.0
            continue
        if atr is not None:
            if np.isnan(atr[i]):
                continue
            out[i] = 1.0 if gap > atr[i] * m else 0.0
        else:
            out[i] = 1.0
    return out


def _gap_down(h, l, c, atr_mult):
    """Bearish 3-bar imbalance (SMC: fair value gap down).

    Mirror of _gap_up — see its docstring for the ATR threshold rationale.
    """
    m = float(atr_mult)
    n = len(h)
    out = _nan(n)
    if n < 3:
        return out
    atr = _atr(h, l, c, 14) if m > 0 else None
    for i in range(2, n):
        if np.isnan(h[i]) or np.isnan(l[i - 2]):
            continue
        gap = l[i - 2] - h[i]
        if gap <= 0:
            out[i] = 0.0
            continue
        if atr is not None:
            if np.isnan(atr[i]):
                continue
            out[i] = 1.0 if gap > atr[i] * m else 0.0
        else:
            out[i] = 1.0
    return out


def _swept_low(l, c, period):
    """Liquidity sweep of a prior low, then a close back above it.

    Fires when the bar's low breaks the lowest low of the preceding `period`
    bars but the bar closes back above that level — a failed breakdown.
    """
    p = int(period)
    n = len(l)
    out = _nan(n)
    if p < 1 or n < p + 1:
        return out
    for i in range(p, n):
        prior = l[i - p:i]
        if np.any(np.isnan(prior)) or np.isnan(l[i]) or np.isnan(c[i]):
            continue
        prior_low = float(prior.min())
        out[i] = 1.0 if (l[i] < prior_low and c[i] > prior_low) else 0.0
    return out


def _swept_high(h, c, period):
    """Liquidity sweep of a prior high, then a close back below it."""
    p = int(period)
    n = len(h)
    out = _nan(n)
    if p < 1 or n < p + 1:
        return out
    for i in range(p, n):
        prior = h[i - p:i]
        if np.any(np.isnan(prior)) or np.isnan(h[i]) or np.isnan(c[i]):
            continue
        prior_high = float(prior.max())
        out[i] = 1.0 if (h[i] > prior_high and c[i] < prior_high) else 0.0
    return out


def _displacement(h, l, c, mult):
    """Outsized-range bar: range exceeds `mult` x the PREVIOUS bar's ATR(14).

    The previous bar's ATR is deliberate. Using the current bar's ATR would
    let a large candle raise the very threshold it has to clear, damping the
    signal this function exists to detect.
    """
    m = float(mult)
    n = len(h)
    out = _nan(n)
    if m <= 0 or n < 16:
        return out
    atr = _atr(h, l, c, 14)
    for i in range(1, n):
        prev_atr = atr[i - 1]
        if np.isnan(prev_atr) or prev_atr <= 0:
            continue
        if np.isnan(h[i]) or np.isnan(l[i]):
            continue
        out[i] = 1.0 if (h[i] - l[i]) > m * prev_atr else 0.0
    return out


def _reversal_close_up(o, c, h, count):
    """`count` consecutive bearish bodies, then a close above their highest high.

    "Bearish body" means close < open. It deliberately does NOT mean
    close < previous close — the two select different bars, and the body
    sense is the one the pattern is defined by.
    """
    k = int(count)
    n = len(c)
    out = _nan(n)
    if k < 1 or n < k + 1:
        return out
    for i in range(k, n):
        lo, hi = i - k, i
        window_o, window_c, window_h = o[lo:hi], c[lo:hi], h[lo:hi]
        if (np.any(np.isnan(window_o)) or np.any(np.isnan(window_c))
                or np.any(np.isnan(window_h)) or np.isnan(c[i])):
            continue
        all_bearish = bool(np.all(window_c < window_o))
        out[i] = 1.0 if (all_bearish and c[i] > float(window_h.max())) else 0.0
    return out


def _reversal_close_down(o, c, l, count):
    """`count` consecutive bullish bodies, then a close below their lowest low."""
    k = int(count)
    n = len(c)
    out = _nan(n)
    if k < 1 or n < k + 1:
        return out
    for i in range(k, n):
        lo, hi = i - k, i
        window_o, window_c, window_l = o[lo:hi], c[lo:hi], l[lo:hi]
        if (np.any(np.isnan(window_o)) or np.any(np.isnan(window_c))
                or np.any(np.isnan(window_l)) or np.isnan(c[i])):
            continue
        all_bullish = bool(np.all(window_c > window_o))
        out[i] = 1.0 if (all_bullish and c[i] < float(window_l.min())) else 0.0
    return out


def _atr_percentile(h, l, c, period=14, lookback=90):
    return _percentile(_atr(h, l, c, int(period)), int(lookback))


def _bbw_percentile(c, period=20, mult=2, lookback=90):
    p, m, lb = int(period), float(mult), int(lookback)
    n = len(c)
    upper = _bb_upper(c, p, m)
    lower = _bb_lower(c, p, m)
    mid = _sma(c, p)
    bbw = _nan(n)
    for i in range(n):
        if not np.isnan(upper[i]) and not np.isnan(lower[i]) and not np.isnan(mid[i]) and mid[i] != 0:
            bbw[i] = ((upper[i] - lower[i]) / mid[i]) * 100
    return _percentile(bbw, lb)


def _bbwidth(c, period=20, mult=2.0):
    """BB Upper minus BB Lower in price units."""
    p, m = int(period), float(mult)
    upper = _bb_upper(c, p, m)
    lower = _bb_lower(c, p, m)
    n = len(c)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(upper[i]) and not np.isnan(lower[i]):
            out[i] = upper[i] - lower[i]
    return out


def _bbwidth_pct(c, period=20, mult=2.0):
    """(BB Upper - BB Lower) / BB Mid * 100 — normalized band width %."""
    p, m = int(period), float(mult)
    upper = _bb_upper(c, p, m)
    lower = _bb_lower(c, p, m)
    mid = _sma(c, p)
    n = len(c)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(upper[i]) and not np.isnan(lower[i]) and not np.isnan(mid[i]) and mid[i] != 0:
            out[i] = (upper[i] - lower[i]) / mid[i] * 100
    return out


def _squeeze_on(h, l, c):
    n = len(h)
    out = _nan(n)
    bb_up = _bb_upper(c, 20, 2)
    bb_lo = _bb_lower(c, 20, 2)
    k_up = _keltner_upper(h, l, c, 20, 1.5)
    k_lo = _keltner_lower(h, l, c, 20, 1.5)
    for i in range(n):
        if not np.isnan(bb_up[i]) and not np.isnan(k_up[i]):
            out[i] = 1.0 if (bb_lo[i] > k_lo[i] and bb_up[i] < k_up[i]) else 0.0
    return out


# ── CPR ────────────────────────────────────────────────────────────────

def _cpr_pivot(h, l, c):
    n = len(h)
    out = _nan(n)
    for i in range(1, n):
        out[i] = (h[i-1] + l[i-1] + c[i-1]) / 3
    return out


def _cpr_r1(h, l, c):
    pv = _cpr_pivot(h, l, c)
    n = len(h)
    out = _nan(n)
    for i in range(1, n):
        if not np.isnan(pv[i]):
            out[i] = 2 * pv[i] - l[i-1]
    return out


def _cpr_r2(h, l, c):
    pv = _cpr_pivot(h, l, c)
    n = len(h)
    out = _nan(n)
    for i in range(1, n):
        if not np.isnan(pv[i]):
            out[i] = pv[i] + (h[i-1] - l[i-1])
    return out


def _cpr_r3(h, l, c):
    pv = _cpr_pivot(h, l, c)
    n = len(h)
    out = _nan(n)
    for i in range(1, n):
        if not np.isnan(pv[i]):
            out[i] = h[i-1] + 2 * (pv[i] - l[i-1])
    return out


def _cpr_s1(h, l, c):
    pv = _cpr_pivot(h, l, c)
    n = len(h)
    out = _nan(n)
    for i in range(1, n):
        if not np.isnan(pv[i]):
            out[i] = 2 * pv[i] - h[i-1]
    return out


def _cpr_s2(h, l, c):
    pv = _cpr_pivot(h, l, c)
    n = len(h)
    out = _nan(n)
    for i in range(1, n):
        if not np.isnan(pv[i]):
            out[i] = pv[i] - (h[i-1] - l[i-1])
    return out


def _cpr_s3(h, l, c):
    pv = _cpr_pivot(h, l, c)
    n = len(h)
    out = _nan(n)
    for i in range(1, n):
        if not np.isnan(pv[i]):
            out[i] = l[i-1] - 2 * (h[i-1] - pv[i])
    return out


# ── Monthly CPR ───────────────────────────────────────────────────
# Mirrors CPRCalculator.calculate_monthly_cpr: for each bar i, find the
# previous calendar month's max-high, min-low, last-close, compute pivot
# levels, and forward-fill until the next month boundary.
# Formulas:  pivot = (H + L + C) / 3
#            bc    = (H + L) / 2
#            tc    = 2 * pivot - bc
#            r1    = 2*pivot - L        r2 = pivot + (H-L)   r3 = r2 + (H-L)
#            s1    = 2*pivot - H        s2 = pivot - (H-L)   s3 = s2 - (H-L)

def _monthly_cpr_levels(h, l, c, dates):
    """Fully vectorized monthly CPR: returns dict of 9 level arrays.

    dates: numpy datetime64[ns] array aligned 1-to-1 with h/l/c.
    Uses UTC month bucketing (same as CPRCalculator.calculate_monthly_cpr).
    For each bar its pivot level is derived from the *previous* calendar month's
    max-H, min-L, and last-C. Forward-filling is implicit because the period
    reindex propagates the same value across all bars of that month.
    """
    _nan_arr = _nan  # module-level helper
    n = len(h)
    empty = {k: _nan_arr(n) for k in ('p', 'tc', 'bc', 'r1', 'r2', 'r3', 's1', 's2', 's3')}

    if dates is None or len(dates) != n or n == 0:
        return empty

    try:
        # ── Build UTC period index (tz-naive — mirrors calculate_monthly_cpr) ──────
        s_idx = pd.DatetimeIndex(dates.astype('datetime64[ns]'), tz=None)
        periods = s_idx.to_period('M')

        # ── Per-month aggregates (groupby on PeriodIndex) ─────────────────
        df_bar = pd.DataFrame({'h': h, 'l': l, 'c': c}, index=periods)
        grp = df_bar.groupby(level=0)
        mon_h = grp['h'].max()
        mon_l = grp['l'].min()
        mon_c = grp['c'].last()

        # ── Shift aggregates by one month to get *previous* month values ──
        # Period.shift(1, 'M') moves each Period forward 1 month, so the
        # value at period M becomes available at period M+1.
        prev_h = mon_h.shift(1, freq='M')
        prev_l = mon_l.shift(1, freq='M')
        prev_c = mon_c.shift(1, freq='M')

        # ── Reindex to bar level via PeriodIndex → aligned numpy arrays ───
        bar_ph = prev_h.reindex(periods).values.astype(np.float64)
        bar_pl = prev_l.reindex(periods).values.astype(np.float64)
        bar_pc = prev_c.reindex(periods).values.astype(np.float64)

        rng_val = bar_ph - bar_pl
        pv = (bar_ph + bar_pl + bar_pc) / 3
        bc = (bar_ph + bar_pl) / 2
        tc = 2 * pv - bc

        return {
            'p':  pv,
            'tc': tc,
            'bc': bc,
            'r1': 2 * pv - bar_pl,
            'r2': pv + rng_val,
            'r3': pv + 2 * rng_val,
            's1': 2 * pv - bar_ph,
            's2': pv - rng_val,
            's3': pv - 2 * rng_val,
        }
    except Exception as e:
        logger.debug(f"monthly_cpr_levels error: {e}")
        return empty


def _monthly_cpr_p(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['p']


def _monthly_cpr_tc(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['tc']


def _monthly_cpr_bc(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['bc']


def _monthly_cpr_r1(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['r1']


def _monthly_cpr_r2(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['r2']


def _monthly_cpr_r3(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['r3']


def _monthly_cpr_s1(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['s1']


def _monthly_cpr_s2(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['s2']


def _monthly_cpr_s3(h, l, c, dates):
    return _monthly_cpr_levels(h, l, c, dates)['s3']


# ── Composite ──────────────────────────────────────────────────────────

def _ema_ribbon_trend(h, l, c):
    n = len(h)
    out = _nan(n)
    e30 = _ema(c, 30)
    e60 = _ema(c, 60)
    a = _atr(h, l, c, 60)
    for i in range(n):
        if np.isnan(e30[i]) or np.isnan(e60[i]) or np.isnan(a[i]):
            continue
        margin = a[i] * 0.3
        diff = e30[i] - e60[i]
        out[i] = 1.0 if diff > margin else (-1.0 if diff < -margin else 0.0)
    return out


def _consensio_fn(c):
    n = len(c)
    out = _nan(n)
    s3 = _sma(c, 3)
    s7 = _sma(c, 7)
    s30 = _sma(c, 30)
    for i in range(n):
        if np.isnan(s3[i]) or np.isnan(s7[i]) or np.isnan(s30[i]) or np.isnan(c[i]):
            continue
        out[i] = float((c[i] > s3[i]) + (c[i] > s7[i]) + (c[i] > s30[i]))
    return out


def _structure_trend(h, l, c):
    n = len(h)
    out = _nan(n)
    if n < 11:
        return out
    swing_high = _nan(n)
    swing_low = _nan(n)
    for i in range(5, n - 5):
        is_high = True
        is_low = True
        for j in range(1, 6):
            if h[i] <= h[i - j] or h[i] <= h[i + j]:
                is_high = False
            if l[i] >= l[i - j] or l[i] >= l[i + j]:
                is_low = False
        if is_high:
            swing_high[i] = h[i]
        if is_low:
            swing_low[i] = l[i]
    last_sh = prev_sh = np.nan
    last_sl = prev_sl = np.nan
    for i in range(n):
        if not np.isnan(swing_high[i]):
            prev_sh = last_sh
            last_sh = swing_high[i]
        if not np.isnan(swing_low[i]):
            prev_sl = last_sl
            last_sl = swing_low[i]
        if np.isnan(prev_sh) or np.isnan(prev_sl):
            out[i] = 0.0
            continue
        hh = last_sh > prev_sh
        hl = last_sl > prev_sl
        lh = last_sh < prev_sh
        ll = last_sl < prev_sl
        out[i] = 1.0 if (hh and hl) else (-1.0 if (lh and ll) else 0.0)
    return out


# ── Math ───────────────────────────────────────────────────────────────

def _abs(src): return np.abs(src)
def _sqrt(src): return np.where(src >= 0, np.sqrt(np.maximum(src, 0)), np.nan)
def _log(src): return np.where(src > 0, np.log(src), np.nan)
def _exp(src): return np.exp(np.clip(src, -700, 700))
def _max_val(a, b): return np.maximum(a, b)
def _min_val(a, b): return np.minimum(a, b)


# ── Trend ──────────────────────────────────────────────────────────────

def _macd_line(src, fast=12, slow=26, sig=9):
    # _ema() uses SMA-seed (Pine-compatible). Do not replace with pandas ewm(adjust=False).
    f = _ema(src, int(fast))
    s = _ema(src, int(slow))
    n = len(src)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(f[i]) and not np.isnan(s[i]):
            out[i] = f[i] - s[i]
    return out


def _macd_signal(src, fast=12, slow=26, sig=9):
    line = _macd_line(src, fast, slow, sig)
    return _ema(line, int(sig))


def _macd_hist(src, fast=12, slow=26, sig=9):
    line = _macd_line(src, fast, slow, sig)
    signal_line = _ema(line, int(sig))
    n = len(src)
    out = _nan(n)
    for i in range(n):
        if not np.isnan(line[i]) and not np.isnan(signal_line[i]):
            out[i] = line[i] - signal_line[i]
    return out


def _obv(c, v):
    n = len(c)
    out = np.zeros(n)
    for i in range(1, n):
        if c[i] > c[i-1]:
            out[i] = out[i-1] + v[i]
        elif c[i] < c[i-1]:
            out[i] = out[i-1] - v[i]
        else:
            out[i] = out[i-1]
    return out


def _supertrend(h, l, c, atr_len, mult):
    n = len(h)
    out = _nan(n)
    a = _atr(h, l, c, int(atr_len))
    start = -1
    for i in range(n):
        if not np.isnan(a[i]):
            start = i
            break
    if start < 0:
        return out
    mid = (h + l) / 2
    upper = mid + a * mult
    lower = mid - a * mult
    direction = 1  # 1=up, -1=down
    final_upper = upper.copy()
    final_lower = lower.copy()
    for i in range(start + 1, n):
        if not np.isnan(final_lower[i-1]) and lower[i] > final_lower[i-1]:
            final_lower[i] = lower[i]
        elif not np.isnan(final_lower[i-1]):
            final_lower[i] = final_lower[i-1]
        if not np.isnan(final_upper[i-1]) and upper[i] < final_upper[i-1]:
            final_upper[i] = upper[i]
        elif not np.isnan(final_upper[i-1]):
            final_upper[i] = final_upper[i-1]
    out[start] = final_lower[start]
    direction = 1
    for i in range(start + 1, n):
        if direction == 1:
            out[i] = final_lower[i]
            if c[i] < final_lower[i]:
                direction = -1
                out[i] = final_upper[i]
        else:
            out[i] = final_upper[i]
            if c[i] > final_upper[i]:
                direction = 1
                out[i] = final_lower[i]
    return out


# ── Function Registry ──────────────────────────────────────────────────
# Format: name -> (callable, type_spec, prefix, defaults)
# type_spec: 'd'=array, 'p'=scalar param

FUNCTION_REGISTRY = {
    # Moving averages
    'sma': (_sma, 'dp', ['close'], {}),
    'ema': (_ema, 'dp', ['close'], {}),
    'wma': (_wma, 'dp', ['close'], {}),
    'hma': (_hma, 'dp', ['close'], {}),
    # Oscillators
    'rsi': (_rsi, 'dp', ['close'], {}),
    # Volatility
    'tr': (_tr, 'ddd', ['high', 'low', 'close'], {}),
    'atr': (_atr, 'dddp', ['high', 'low', 'close'], {}),
    'stdev': (_stdev, 'dp', ['close'], {}),
    'bb_upper': (_bb_upper, 'dpp', ['close'], {}),
    'bb_lower': (_bb_lower, 'dpp', ['close'], {}),
    'bb_mid': (_bb_mid, 'dp', ['close'], {}),
    'keltner_upper': (_keltner_upper, 'dddpp', ['high', 'low', 'close'], {}),
    'keltner_lower': (_keltner_lower, 'dddpp', ['high', 'low', 'close'], {}),
    # Trend
    'supertrend': (_supertrend, 'dddpp', ['high', 'low', 'close'], {}),
    'macd_line': (_macd_line, 'dppp', ['close'], {1: 12, 2: 26, 3: 9}),
    'macd_signal': (_macd_signal, 'dppp', ['close'], {1: 12, 2: 26, 3: 9}),
    'macd_hist': (_macd_hist, 'dppp', ['close'], {1: 12, 2: 26, 3: 9}),
    # Volume
    'obv': (_obv, 'dd', ['close', 'volume'], {}),
    # Bar reference
    'highest': (_highest, 'dp', ['high'], {}),
    'lowest': (_lowest, 'dp', ['low'], {}),
    'sum': (_sum, 'dp', ['close'], {}),
    'change': (_change, 'dp', ['close'], {1: 1}),
    'roc': (_roc, 'dp', ['close'], {}),
    'mom': (_change, 'dp', ['close'], {1: 1}),
    'offset': (_offset, 'dp', ['close'], {}),
    'ref':    (_offset, 'dp', ['close'], {}),  # alias for OFFSET (REF(x,n) = x n bars ago)
    # Logic / signals
    'crossover': (_crossover, 'dd', [], {}),
    'crossunder': (_crossunder, 'dd', [], {}),
    'valuewhen': (_valuewhen, 'dd', [], {}),
    'barssince': (_barssince, 'd', [], {}),
    'count': (_count, 'dp', [], {}),
    'cum': (_cum, 'd', ['close'], {}),
    'streak': (_streak, 'd', [], {}),
    # Structure
    'highest_bars': (_highest_bars, 'dp', ['high'], {}),
    'lowest_bars': (_lowest_bars, 'dp', ['low'], {}),
    'pivothigh': (_pivothigh, 'dpp', ['high'], {1: 5, 2: 5}),
    'pivotlow': (_pivotlow, 'dpp', ['low'], {1: 5, 2: 5}),
    # Math
    'abs': (_abs, 'd', [], {}),
    'sqrt': (_sqrt, 'd', [], {}),
    'log': (_log, 'd', [], {}),
    'exp': (_exp, 'd', [], {}),
    'max_val': (_max_val, 'dd', [], {}),
    'min_val': (_min_val, 'dd', [], {}),
    # Quant
    'zscore': (_zscore, 'dp', ['close'], {}),
    'percentile': (_percentile, 'dp', ['close'], {}),
    'corr': (_corr, 'ddp', [], {}),
    'beta': (_beta, 'ddp', [], {}),
    # Ichimoku
    'tenkan': (_tenkan, 'ddp', ['high', 'low'], {2: 9}),
    'kijun': (_kijun, 'ddp', ['high', 'low'], {2: 26}),
    'senkou_a': (_senkou_a, 'dd', ['high', 'low'], {}),
    'senkou_b': (_senkou_b, 'ddp', ['high', 'low'], {2: 52}),
    'chikou': (_chikou, 'd', ['close'], {}),
    # Alligator
    'alligator_jaw': (_alligator_jaw, 'ddpp', ['high', 'low'], {2: 13, 3: 8}),
    'alligator_teeth': (_alligator_teeth, 'ddpp', ['high', 'low'], {2: 8, 3: 5}),
    'alligator_lips': (_alligator_lips, 'ddpp', ['high', 'low'], {2: 5, 3: 3}),
    # Stoch RSI
    'stoch_rsi_k': (_stoch_rsi_k, 'dpp', ['close'], {1: 14, 2: 3}),
    'stoch_rsi_d': (_stoch_rsi_d, 'dppp', ['close'], {1: 14, 2: 3, 3: 3}),
    # Donchian
    'donchian_high': (_donchian_high, 'dp', ['high'], {1: 20}),
    'donchian_low': (_donchian_low, 'dp', ['low'], {1: 20}),
    # Detection
    'nr7': (_nr7, 'dd', ['high', 'low'], {}),
    # SMC pattern primitives
    'gap_up': (_gap_up, 'dddp', ['high', 'low', 'close'], {3: 0.25}),
    'gap_down': (_gap_down, 'dddp', ['high', 'low', 'close'], {3: 0.25}),
    'swept_low': (_swept_low, 'ddp', ['low', 'close'], {2: 20}),
    'swept_high': (_swept_high, 'ddp', ['high', 'close'], {2: 20}),
    'displacement': (_displacement, 'dddp', ['high', 'low', 'close'], {3: 2.0}),
    'reversal_close_up': (_reversal_close_up, 'dddp', ['open', 'close', 'high'], {3: 2}),
    'reversal_close_down': (_reversal_close_down, 'dddp', ['open', 'close', 'low'], {3: 2}),
    # SMC vocabulary aliases — same callables, community-familiar names.
    # Precedent: 'ref' is already an alias for _offset.
    # fvg_* earns its name: GAP_UP's 0.25 default matches the FVG chart
    # indicator's atrThresholdPct, so the DSL and the chart agree.
    # 'order_block' is deliberately absent — Carrac draws structure-anchored
    # OB zones with mitigation (smart-money-concepts.js detectOrderBlocks);
    # no candle-sequence formula here is that, and borrowing the name would
    # create a fourth meaning (an earlier collision shows what that costs).
    'fvg_up': (_gap_up, 'dddp', ['high', 'low', 'close'], {3: 0.25}),
    'fvg_down': (_gap_down, 'dddp', ['high', 'low', 'close'], {3: 0.25}),
    'liquidity_sweep_low': (_swept_low, 'ddp', ['low', 'close'], {2: 20}),
    'liquidity_sweep_high': (_swept_high, 'ddp', ['high', 'close'], {2: 20}),
    'csd_up': (_reversal_close_up, 'dddp', ['open', 'close', 'high'], {3: 2}),
    'csd_down': (_reversal_close_down, 'dddp', ['open', 'close', 'low'], {3: 2}),
    'atr_percentile': (_atr_percentile, 'dddpp', ['high', 'low', 'close'], {3: 14, 4: 90}),
    'bbw_percentile': (_bbw_percentile, 'dppp', ['close'], {1: 20, 2: 2, 3: 90}),
    'bbwidth':        (_bbwidth,        'dpp',  ['close'], {1: 20, 2: 2.0}),     # price-unit BB width
    'bbwidth_pct':    (_bbwidth_pct,    'dpp',  ['close'], {1: 20, 2: 2.0}),     # % normalized BB width
    'squeeze_on': (_squeeze_on, 'ddd', ['high', 'low', 'close'], {}),
    # CPR (daily-rolling)
    'cpr_pivot': (_cpr_pivot, 'ddd', ['high', 'low', 'close'], {}),
    'cpr_r1': (_cpr_r1, 'ddd', ['high', 'low', 'close'], {}),
    'cpr_r2': (_cpr_r2, 'ddd', ['high', 'low', 'close'], {}),
    'cpr_r3': (_cpr_r3, 'ddd', ['high', 'low', 'close'], {}),
    'cpr_s1': (_cpr_s1, 'ddd', ['high', 'low', 'close'], {}),
    'cpr_s2': (_cpr_s2, 'ddd', ['high', 'low', 'close'], {}),
    'cpr_s3': (_cpr_s3, 'ddd', ['high', 'low', 'close'], {}),
    # Monthly CPR — anchored to previous calendar month's H/L/C
    # '__dates__' in prefix is resolved as raw datetime64[ns] from context
    'monthly_cpr_p':  (_monthly_cpr_p,  'dddd', ['high', 'low', 'close', '__dates__'], {}),
    'monthly_pivot':  (_monthly_cpr_p,  'dddd', ['high', 'low', 'close', '__dates__'], {}),  # alias
    'monthly_cpr_tc': (_monthly_cpr_tc, 'dddd', ['high', 'low', 'close', '__dates__'], {}),
    'monthly_cpr_bc': (_monthly_cpr_bc, 'dddd', ['high', 'low', 'close', '__dates__'], {}),
    'monthly_cpr_r1': (_monthly_cpr_r1, 'dddd', ['high', 'low', 'close', '__dates__'], {}),
    'monthly_cpr_r2': (_monthly_cpr_r2, 'dddd', ['high', 'low', 'close', '__dates__'], {}),
    'monthly_cpr_r3': (_monthly_cpr_r3, 'dddd', ['high', 'low', 'close', '__dates__'], {}),
    'monthly_cpr_s1': (_monthly_cpr_s1, 'dddd', ['high', 'low', 'close', '__dates__'], {}),
    'monthly_cpr_s2': (_monthly_cpr_s2, 'dddd', ['high', 'low', 'close', '__dates__'], {}),
    'monthly_cpr_s3': (_monthly_cpr_s3, 'dddd', ['high', 'low', 'close', '__dates__'], {}),
    # Composite
    'ema_ribbon_trend': (_ema_ribbon_trend, 'ddd', ['high', 'low', 'close'], {}),
    'consensio': (_consensio_fn, 'd', ['close'], {}),
    'structure_trend': (_structure_trend, 'ddd', ['high', 'low', 'close'], {}),
}


# ── Tokenizer ──────────────────────────────────────────────────────────
# String literals ('...' or "...") and `.` for member access.
# A leading `.` followed by a digit is still a number (e.g., `.5`); a `.`
# followed by a letter is member access.

_TOKEN_RE = re.compile(r'''
    (\d+\.?\d*|\.\d+)          |  # NUMBER
    (?:'([^']*)'|"([^"]*)")    |  # STRING (group 2 single, group 3 double)
    ([a-zA-Z_]\w*)             |  # IDENT (group 4)
    (>=|<=|==|!=)              |  # CMP2 (group 5)
    ([+\-*/%><=()[\],.])          # SINGLE (group 6) — includes `.` for members
''', re.VERBOSE)


def tokenize(formula: str) -> list:
    # Strip comments
    formula = re.sub(r'(//|#).*', '', formula)
    tokens = []
    for m in _TOKEN_RE.finditer(formula):
        if m.group(1):
            tokens.append(('NUM', float(m.group(1))))
        elif m.group(2) is not None or m.group(3) is not None:
            tokens.append(('STR', m.group(2) if m.group(2) is not None else m.group(3)))
        elif m.group(4):
            tokens.append(('ID', m.group(4).lower()))
        elif m.group(5):
            tokens.append(('OP', m.group(5)))
        elif m.group(6):
            tokens.append(('OP', m.group(6)))
    tokens.append(('EOF', None))
    return tokens


# ── Parser ─────────────────────────────────────────────────────────────
# Recursive descent, same precedence as JS version.

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ('EOF', None)

    def eat(self, expected_val=None):
        tok = self.peek()
        if expected_val is not None and tok[1] != expected_val:
            raise ValueError(f"Expected '{expected_val}', got '{tok[1]}'")
        self.pos += 1
        return tok

    def parse(self):
        """Parse a formula expression into an AST."""
        # Handle let bindings
        bindings = []
        while self.peek()[0] == 'ID' and self.peek()[1] == 'let':
            self.eat()  # 'let'
            name_tok = self.eat()
            if name_tok[0] != 'ID':
                raise ValueError("Expected identifier after 'let'")
            self.eat('=')
            expr = self.expr()
            bindings.append((name_tok[1], expr))
            if len(bindings) > 10:
                raise ValueError("Too many let bindings (max 10)")

        body = self.expr()
        # Reject trailing garbage. Without this, `expr()` parses the first
        # expression and any unconsumed tokens are silently dropped, so a
        # malformed input like `this is not valid (((` parses as `this`.
        if self.peek()[0] != 'EOF':
            tok = self.peek()
            raise ValueError(
                f"Unexpected token after expression: {tok[1]!r}"
            )
        if bindings:
            return {'type': 'program', 'bindings': bindings, 'body': body}
        return body

    def expr(self):
        return self.logical()

    def logical(self):
        left = self.comparison()
        while self.peek()[0] == 'ID' and self.peek()[1] in ('and', 'or'):
            op = self.eat()[1]
            right = self.comparison()
            left = {'type': 'bin', 'op': op, 'left': left, 'right': right}
        return left

    def comparison(self):
        left = self.additive()
        while self.peek()[1] in ('>', '<', '>=', '<=', '==', '!='):
            op = self.eat()[1]
            right = self.additive()
            left = {'type': 'bin', 'op': op, 'left': left, 'right': right}
        return left

    def additive(self):
        left = self.multiplicative()
        while self.peek()[1] in ('+', '-'):
            op = self.eat()[1]
            right = self.multiplicative()
            left = {'type': 'bin', 'op': op, 'left': left, 'right': right}
        return left

    def multiplicative(self):
        left = self.unary()
        while self.peek()[1] in ('*', '/', '%'):
            op = self.eat()[1]
            right = self.unary()
            left = {'type': 'bin', 'op': op, 'left': left, 'right': right}
        return left

    def unary(self):
        if self.peek()[1] == '-':
            self.eat()
            operand = self.unary()
            return {'type': 'un', 'op': '-', 'operand': operand}
        if self.peek()[0] == 'ID' and self.peek()[1] == 'not':
            self.eat()
            operand = self.unary()
            return {'type': 'un', 'op': 'not', 'operand': operand}
        return self.postfix()

    def postfix(self):
        node = self.primary()
        # Chain `[offset]` and `.member` suffixes
        while True:
            t = self.peek()
            if t[1] == '[':
                self.eat()
                idx = self.expr()
                self.eat(']')
                node = {'type': 'index', 'base': node, 'index': idx}
            elif t[1] == '.':
                self.eat()
                member_tok = self.eat()
                if member_tok[0] != 'ID':
                    raise ValueError(f"Expected member name after '.', got '{member_tok[1]}'")
                node = {'type': 'member', 'base': node, 'member': member_tok[1]}
            else:
                break
        return node

    def primary(self):
        tok = self.peek()
        if tok[0] == 'NUM':
            self.eat()
            return {'type': 'num', 'v': tok[1]}
        if tok[0] == 'STR':
            self.eat()
            return {'type': 'str', 'v': tok[1]}
        if tok[0] == 'ID':
            name = tok[1]
            self.eat()
            if self.peek()[1] == '(':
                # Function call
                self.eat()
                args = []
                if self.peek()[1] != ')':
                    args.append(self.expr())
                    while self.peek()[1] == ',':
                        self.eat()
                        args.append(self.expr())
                self.eat(')')
                return {'type': 'call', 'name': name, 'args': args}
            return {'type': 'id', 'name': name}
        if tok[1] == '(':
            self.eat()
            node = self.expr()
            self.eat(')')
            return node
        raise ValueError(f"Unexpected token: {tok}")


# ── Levenshtein helper for "did you mean" suggestions ──────────────

def _levenshtein(a: str, b: str) -> int:
    """Compute edit distance between two lowercase strings (no deps)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1,
                          prev[j - 1] + (0 if ca == cb else 1))
        prev = curr
    return prev[-1]


def _levenshtein_suggest(name: str, candidates: list, n: int = 3, max_dist: int = 3) -> list:
    """Return up to n candidates closest to name (case-insensitive) within max_dist."""
    name_l = name.lower()
    scored = [(c, _levenshtein(name_l, c.lower())) for c in candidates]
    scored = [(c, d) for c, d in scored if d <= max_dist]
    scored.sort(key=lambda x: x[1])
    return [c for c, _ in scored[:n]]


# ── Evaluator ──────────────────────────────────────────────────────────

class Evaluator:
    """Walks AST and produces numpy arrays. Mirrors JS evaluator semantics."""

    def __init__(self, context: dict, n: int, symbol: str = '', timeframe: str = '1D', htf_depth: int = 0):
        self.context = context  # {open, high, low, close, volume, _dates, ...}
        self.n = n
        self.symbol = symbol
        self.timeframe = timeframe
        self.htf_depth = htf_depth  # guards against nested HTF
        self.call_count = 0
        self.let_vars = {}  # let bindings
        self.start_time = time.time()

    def _check_timeout(self):
        if (time.time() - self.start_time) * 1000 > EVAL_TIMEOUT_MS:
            raise TimeoutError("Formula took too long to evaluate")

    def eval(self, node) -> tuple:
        """Returns (type, value) where type is 's' (scalar) or 'a' (array)."""
        self._check_timeout()

        ntype = node['type']

        if ntype == 'program':
            for name, expr in node['bindings']:
                self.let_vars[name] = self.eval(expr)
            return self.eval(node['body'])

        if ntype == 'num':
            return ('s', node['v'])

        if ntype == 'str':
            # String literal — only valid as function arg, not math.
            return ('str', node['v'])

        if ntype == 'id':
            return self._resolve_ident(node['name'])

        if ntype == 'call':
            return self._eval_call(node)

        if ntype == 'un':
            val = self.eval(node['operand'])
            arr = self._to_arr(val)
            if node['op'] == '-':
                return ('a', -arr)
            if node['op'] == 'not':
                out = np.where(np.isnan(arr), np.nan, np.where(arr != 0, 0.0, 1.0))
                return ('a', out)

        if ntype == 'bin':
            return self._eval_bin(node)

        if ntype == 'index':
            base = self._to_arr(self.eval(node['base']))
            idx_r = self.eval(node['index'])
            if idx_r[0] != 's':
                raise ValueError("Bar offset must be a number")
            return ('a', self._offset_arr(base, int(idx_r[1])))

        if ntype == 'member':
            # Dot-access — only legal on a 'dict' result returned
            # by one of the QE primitives (leadlag, chain). The parser lets
            # `.name` attach to any postfix, so we defend here.
            base = self.eval(node['base'])
            if base[0] != 'dict':
                raise ValueError(f"'.{node['member']}' not valid on non-dict value")
            d = base[1]
            if node['member'] not in d:
                raise ValueError(
                    f"Unknown member '.{node['member']}'. "
                    f"Available: {', '.join(sorted(d.keys()))}")
            v = d[node['member']]
            if v is None:
                return ('s', float('nan'))
            return ('s', float(v))

        raise ValueError(f"Unknown AST node type: {ntype}")

    def _resolve_ident(self, name):
        # Check let bindings
        if name in self.let_vars:
            return self.let_vars[name]
        # OHLCV + quant data
        if name in self.context:
            arr = self.context[name]
            if isinstance(arr, np.ndarray):
                return ('a', arr)
            return ('s', float(arr))
        # Computed shorthands
        if name == 'hl2':
            arr = (self.context['high'] + self.context['low']) / 2
            self.context['hl2'] = arr
            return ('a', arr)
        if name == 'hlc3':
            arr = (self.context['high'] + self.context['low'] + self.context['close']) / 3
            self.context['hlc3'] = arr
            return ('a', arr)
        if name == 'ohlc4':
            arr = (self.context['open'] + self.context['high'] + self.context['low'] + self.context['close']) / 4
            self.context['ohlc4'] = arr
            return ('a', arr)
        if name == 'tr':
            arr = _tr(self.context['high'], self.context['low'], self.context['close'])
            self.context['tr'] = arr
            return ('a', arr)
        # Improved error — check if the name is a known function (needs parentheses)
        if name in FUNCTION_REGISTRY:
            raise ValueError(
                f"Unknown identifier '{name.upper()}'. "
                f"Did you mean `{name}(...)` (it's a function — add parentheses)? "
                f"Call indicator_get_dsl_reference for the full list."
            )
        # Check for close matches among known series names
        _OHLCV_NAMES = ('close', 'high', 'low', 'open', 'volume', 'hl2', 'hlc3', 'ohlc4', 'tr')
        _candidates = list(QUANT_VARS.keys()) + list(DERIV_VARS.keys()) + list(_OHLCV_NAMES)
        suggestions = _levenshtein_suggest(name, _candidates, n=3, max_dist=3)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        raise ValueError(
            f"Unknown identifier '{name.upper()}'.{hint} "
            f"Call indicator_get_dsl_reference for the full list."
        )

    def _eval_call(self, node):
        name = node['name']
        args = node['args']

        # Special functions: IF, AND, OR, NOT
        if name == 'if':
            if len(args) != 3:
                raise ValueError("IF expects 3 arguments")
            cond = self._to_arr(self.eval(args[0]))
            then = self._to_arr(self.eval(args[1]))
            else_ = self._to_arr(self.eval(args[2]))
            out = np.where(~np.isnan(cond) & (cond != 0), then, else_)
            return ('a', out)
        if name == 'and':
            if len(args) != 2:
                raise ValueError("AND expects 2 arguments")
            a = self._to_arr(self.eval(args[0]))
            b = self._to_arr(self.eval(args[1]))
            out = np.where(np.isnan(a) | np.isnan(b), np.nan,
                           np.where((a != 0) & (b != 0), 1.0, 0.0))
            return ('a', out)
        if name == 'or':
            if len(args) != 2:
                raise ValueError("OR expects 2 arguments")
            a = self._to_arr(self.eval(args[0]))
            b = self._to_arr(self.eval(args[1]))
            out = np.where(np.isnan(a) | np.isnan(b), np.nan,
                           np.where((a != 0) | (b != 0), 1.0, 0.0))
            return ('a', out)
        if name == 'not':
            if len(args) != 1:
                raise ValueError("NOT expects 1 argument")
            a = self._to_arr(self.eval(args[0]))
            out = np.where(np.isnan(a), np.nan, np.where(a != 0, 0.0, 1.0))
            return ('a', out)

        # ── Chain-strength primitives ──────────────────────────────────────────
        # These return scalars (or dicts for member-access) from the nightly
        # quant engine. Each hits the DB at most once per formula; results
        # are broadcast to the output array via _to_arr when used in math.
        if name in ('leadlag', 'chain', 'regime_prob', 'anomaly_active',
                    'analogue_similarity'):
            return self._eval_qe_primitive(name, args)

        # ── HTF (higher timeframe) / MTF alias ────────────────────────
        # HTF('1W', rsi(14)) → evaluate the sub-expression against OHLCV
        # of the target TF, then align (forward-fill) back to the caller's
        # bar index using _dates.
        # MTF is an alias for HTF — same signature, same semantics.
        if name in ('htf', 'mtf'):
            return self._eval_htf(args)

        # ── DIVERGENCE ─────────────────────────────────────────────────
        # divergence(price, osc, lookback=60) → 1.0 at confirmed bearish,
        # -1.0 at confirmed bullish, 0.0 elsewhere.
        if name == 'divergence':
            return self._eval_divergence(args)

        # Registry lookup — improved error with "did you mean" suggestions
        if name not in FUNCTION_REGISTRY:
            suggestions = _levenshtein_suggest(name, list(FUNCTION_REGISTRY.keys()), n=3, max_dist=3)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise ValueError(
                f"Unknown function '{name.upper()}'.{hint} "
                f"Call indicator_get_dsl_reference for the full list."
            )

        self.call_count += 1
        if self.call_count > MAX_FUNCTION_CALLS:
            raise ValueError(f"Too many function calls (max {MAX_FUNCTION_CALLS})")

        fn, types, prefix, defaults = FUNCTION_REGISTRY[name]

        # Evaluate user args
        user_args = [self.eval(a) for a in args]
        resolved = self._resolve_args(name, types, prefix, defaults, user_args)
        result = fn(*resolved)
        if isinstance(result, np.ndarray):
            return ('a', result)
        return ('s', float(result))

    def _resolve_args(self, name, types, prefix, defaults, user_args):
        """Resolve arguments with prefix auto-prepend and defaults."""
        total = len(types)
        user_n = len(user_args)
        num_defaults = len(defaults)

        if user_n > total:
            raise ValueError(f"{name.upper()} expects at most {total} arguments, got {user_n}")

        # Shorthand detection: first arg is scalar and prefix available
        need_prefix = (len(prefix) > 0 and user_n < total and
                       (user_n == 0 or user_args[0][0] == 's'))

        resolved = []

        # Prepend prefix if needed
        if need_prefix:
            for p in prefix:
                if p == '__dates__':
                    # Pass raw datetime64[ns] array from context — NOT float-converted
                    resolved.append(self.context.get('_dates'))
                else:
                    r = self._resolve_ident(p)
                    resolved.append(self._to_arr(r))

        # Add user args with type conversion
        for i, ua in enumerate(user_args):
            pos = len(resolved)
            if pos >= total:
                raise ValueError(f"{name.upper()} too many arguments")
            if types[pos] == 'd':
                resolved.append(self._to_arr(ua))
            else:  # 'p'
                if ua[0] != 's':
                    raise ValueError(f"{name.upper()}: argument {i+1} must be a number")
                resolved.append(ua[1])

        # Fill trailing defaults
        while len(resolved) < total:
            pos = len(resolved)
            if pos in defaults:
                resolved.append(defaults[pos])
            else:
                min_user = total - len(prefix) - num_defaults
                raise ValueError(f"{name.upper()} expects at least {min_user} argument(s)")

        return resolved

    def _eval_bin(self, node):
        left = self.eval(node['left'])
        right = self.eval(node['right'])
        op = node['op']

        # Both scalars → scalar result
        if left[0] == 's' and right[0] == 's':
            a, b = left[1], right[1]
            if op == 'and':
                return ('s', 1.0 if (a != 0 and b != 0) else 0.0)
            if op == 'or':
                return ('s', 1.0 if (a != 0 or b != 0) else 0.0)
            result = self._scalar_op(op, a, b)
            return ('s', result)

        # At least one array → array result
        a = self._to_arr(left)
        b = self._to_arr(right)

        if op == 'and':
            return ('a', np.where(np.isnan(a) | np.isnan(b), np.nan,
                                  np.where((a != 0) & (b != 0), 1.0, 0.0)))
        if op == 'or':
            return ('a', np.where(np.isnan(a) | np.isnan(b), np.nan,
                                  np.where((a != 0) | (b != 0), 1.0, 0.0)))

        with np.errstate(divide='ignore', invalid='ignore'):
            if op == '+':
                return ('a', a + b)
            if op == '-':
                return ('a', a - b)
            if op == '*':
                return ('a', a * b)
            if op == '/':
                return ('a', np.where(b == 0, np.nan, a / b))
            if op == '%':
                return ('a', np.where(b == 0, np.nan, np.fmod(a, b)))
            if op == '>':
                return ('a', np.where(np.isnan(a) | np.isnan(b), np.nan,
                                      np.where(a > b, 1.0, 0.0)))
            if op == '<':
                return ('a', np.where(np.isnan(a) | np.isnan(b), np.nan,
                                      np.where(a < b, 1.0, 0.0)))
            if op == '>=':
                return ('a', np.where(np.isnan(a) | np.isnan(b), np.nan,
                                      np.where(a >= b, 1.0, 0.0)))
            if op == '<=':
                return ('a', np.where(np.isnan(a) | np.isnan(b), np.nan,
                                      np.where(a <= b, 1.0, 0.0)))
            if op == '==':
                return ('a', np.where(np.isnan(a) | np.isnan(b), np.nan,
                                      np.where(a == b, 1.0, 0.0)))
            if op == '!=':
                return ('a', np.where(np.isnan(a) | np.isnan(b), np.nan,
                                      np.where(a != b, 1.0, 0.0)))

        raise ValueError(f"Unknown operator: {op}")

    def _scalar_op(self, op, a, b):
        if op == '+': return a + b
        if op == '-': return a - b
        if op == '*': return a * b
        if op == '/': return float('nan') if b == 0 else a / b
        if op == '%': return float('nan') if b == 0 else math.fmod(a, b)
        if op == '>': return 1.0 if a > b else 0.0
        if op == '<': return 1.0 if a < b else 0.0
        if op == '>=': return 1.0 if a >= b else 0.0
        if op == '<=': return 1.0 if a <= b else 0.0
        if op == '==': return 1.0 if a == b else 0.0
        if op == '!=': return 1.0 if a != b else 0.0
        raise ValueError(f"Unknown operator: {op}")

    def _to_arr(self, result):
        """Convert scalar to broadcast array."""
        if result[0] == 'a':
            return result[1]
        if result[0] == 'dict':
            raise ValueError(
                "Cannot use a dict value directly — use member access (e.g., leadlag('M2','BTC').projected_move)")
        if result[0] == 'str':
            raise ValueError("Cannot use a string literal in math — only as a function argument")
        return np.full(self.n, result[1])

    def _eval_qe_primitive(self, name, args):
        """Dot-notation primitives. Each pulls a single value from
        the quant DB and returns either a scalar tuple ('s', v) or a dict
        tuple ('dict', {...}) for member access.
        """
        self.call_count += 1
        if self.call_count > MAX_FUNCTION_CALLS:
            raise ValueError(f"Too many function calls (max {MAX_FUNCTION_CALLS})")

        def _str_arg(node, pos, fn_name):
            ev = self.eval(node)
            if ev[0] != 'str':
                raise ValueError(f"{fn_name.upper()}: arg {pos+1} must be a string literal (e.g., 'BTC')")
            return ev[1]

        def _scalar_arg(node, pos, fn_name):
            ev = self.eval(node)
            if ev[0] != 's':
                raise ValueError(f"{fn_name.upper()}: arg {pos+1} must be a number")
            return float(ev[1])

        try:
            from carrac.db_pool import get_quant_conn as get_conn
        except Exception as e:
            raise ValueError(f"QE unavailable: {e}")

        if name == 'leadlag':
            if len(args) != 2:
                raise ValueError("LEADLAG expects 2 arguments: leader, target")
            leader = _str_arg(args[0], 0, 'leadlag')
            target = _str_arg(args[1], 1, 'leadlag')
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT window_days AS lag, correlation, ci_low, ci_high
                    FROM quant.correlations
                    WHERE metric_a = %s AND metric_b = %s AND method = 'leadlag'
                      AND ts = (SELECT MAX(ts) FROM quant.correlations WHERE method = 'leadlag')
                    ORDER BY ABS(correlation) DESC LIMIT 1
                """, (leader, target))
                row = cur.fetchone()
                if not row:
                    return ('dict', {
                        'r': None, 'lag': None, 'projected_move': None,
                        'ci_low': None, 'ci_high': None,
                    })
                r_val = float(row['correlation'])
                lag = int(row['lag'])
                ci_low = float(row['ci_low']) if row.get('ci_low') is not None else None
                ci_high = float(row['ci_high']) if row.get('ci_high') is not None else None
                # Leader z-score (90d window)
                cur.execute("""
                    SELECT zscore FROM quant.zscores
                    WHERE metric = %s AND window_days = 90
                    ORDER BY ts DESC LIMIT 1
                """, (leader,))
                z = cur.fetchone()
                leader_z = float(z['zscore']) if z else None
                projected = round(r_val * leader_z, 4) if leader_z is not None else None
            return ('dict', {
                'r': round(r_val, 4),
                'lag': lag,
                'leader_z': round(leader_z, 3) if leader_z is not None else None,
                'projected_move': projected,
                'ci_low': ci_low,
                'ci_high': ci_high,
            })

        if name == 'chain':
            if len(args) != 1:
                raise ValueError("CHAIN expects 1 argument: chain_name")
            cname = _str_arg(args[0], 0, 'chain')
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT value, metadata FROM quant.signals
                    WHERE source = 'chain_strength' AND metric = %s AND asset = '_GLOBAL_'
                    ORDER BY ts DESC LIMIT 1
                """, (cname,))
                row = cur.fetchone()
                if not row:
                    return ('dict', {
                        'strength': None, 'weakest_r': None,
                        'weakest_from': None, 'weakest_to': None,
                    })
                meta = row['metadata'] or {}
                if isinstance(meta, str):
                    import json as _json
                    meta = _json.loads(meta)
                wk = meta.get('weakest_link') or {}
            return ('dict', {
                'strength': round(float(row['value']), 4),
                'weakest_r': wk.get('r'),
                'weakest_from': wk.get('from'),
                'weakest_to': wk.get('to'),
            })

        if name == 'regime_prob':
            # regime_prob('favorable') → probability the current regime matches
            # 'favorable' (1.0 if labels match, HMM probability; 0 otherwise).
            if len(args) != 1:
                raise ValueError("REGIME_PROB expects 1 argument: state_name")
            state = _str_arg(args[0], 0, 'regime_prob').lower()
            if state not in ('favorable', 'unfavorable'):
                raise ValueError("REGIME_PROB: state must be 'favorable' or 'unfavorable'")
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT metadata FROM quant.signals
                    WHERE source = 'regime_hmm' AND metric = 'REGIME' AND asset = '_GLOBAL_'
                    ORDER BY ts DESC LIMIT 1
                """)
                row = cur.fetchone()
                if not row:
                    return ('s', float('nan'))
                meta = row['metadata'] or {}
                if isinstance(meta, str):
                    import json as _json
                    meta = _json.loads(meta)
                label = (meta.get('label') or '').lower()
                prob = float(meta.get('probability') or 0)
            return ('s', prob if label == state else 0.0)

        if name == 'anomaly_active':
            # anomaly_active() → 1.0 if an unresolved multi-domain cluster
            # currently exists, else 0.
            if len(args) != 0:
                raise ValueError("ANOMALY_ACTIVE expects no arguments")
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT 1 FROM quant.anomalies
                    WHERE domain = 'cluster' AND metric = 'MULTI_DOMAIN'
                      AND resolved_at IS NULL
                    LIMIT 1
                """)
                return ('s', 1.0 if cur.fetchone() else 0.0)

        if name == 'analogue_similarity':
            # analogue_similarity() or analogue_similarity(rank) →
            # cosine similarity of the Nth closest analogue (1-indexed, default 1).
            rank = 1 if not args else int(_scalar_arg(args[0], 0, 'analogue_similarity'))
            if rank < 1 or rank > 10:
                raise ValueError("ANALOGUE_SIMILARITY: rank must be between 1 and 10")
            try:
                from quant.engine.conditional import query_analogues
                res = query_analogues(target='BTC', top_n=rank)
                items = res.get('analogues', []) if res else []
                if len(items) < rank:
                    return ('s', float('nan'))
                sim = items[rank - 1].get('similarity')
                return ('s', float(sim) if sim is not None else float('nan'))
            except Exception as e:
                raise ValueError(f"ANALOGUE_SIMILARITY failed: {e}")

        raise ValueError(f"Unknown QE primitive: {name}")

    def _eval_htf(self, args):
        """HTF(target_tf, sub_expr) — evaluate sub_expr on a higher-timeframe
        OHLCV series, then align back to caller's bar index via forward-fill."""
        if len(args) != 2:
            raise ValueError("HTF expects 2 arguments: HTF(target_tf, expression)")
        if self.htf_depth >= 1:
            raise ValueError("HTF cannot be nested")
        # Reject nested HTF up-front by inspecting the sub-expression AST.
        # Without this, `_eval_htf` short-circuits when target-TF OHLCV is
        # absent and never recurses into args[1], so the depth guard above
        # would never fire for the inner call.
        if _ast_contains_call(args[1], 'htf'):
            raise ValueError("HTF cannot be nested")

        # First arg must be a string literal (evaluate only it — sub_expr is deferred)
        tf_result = self.eval(args[0])
        if tf_result[0] != 'str':
            raise ValueError("HTF arg 1 must be a timeframe string literal, e.g., '1W'")
        target_tf = tf_result[1]

        if target_tf not in TF_ORDER:
            raise ValueError(f"HTF unknown timeframe '{target_tf}'. "
                             f"Valid: {', '.join(sorted(TF_ORDER.keys(), key=TF_ORDER.get))}")
        # Refuse lower/equal TF — prevents forward-looking bias and no-op usage.
        caller_rank = TF_ORDER.get(self.timeframe, 3)
        target_rank = TF_ORDER.get(target_tf, 3)
        if target_rank <= caller_rank:
            raise ValueError(
                f"HTF target ('{target_tf}') must be higher than caller timeframe "
                f"('{self.timeframe}'). Use a longer timeframe or remove HTF.")

        if not self.symbol:
            raise ValueError("HTF requires a symbol context")

        htf_ohlcv = load_ohlcv(self.symbol, target_tf)
        if htf_ohlcv is None or len(htf_ohlcv.get('close', [])) == 0:
            # No data at target TF — return NaN array
            return ('a', _nan(self.n))

        # Evaluate sub_expr in isolated HTF context
        htf_n = len(htf_ohlcv['close'])
        htf_context = dict(htf_ohlcv)

        # Carry over any non-OHLCV entries from caller (e.g., injected quant vars
        # that are global-scope, not per-TF). Skip per-TF arrays that will differ.
        carry_keys = set()
        for k, v in self.context.items():
            if k in ('open', 'high', 'low', 'close', 'volume', '_dates', '_total_available'):
                continue
            # Scalars carry trivially; arrays are only meaningful if same-length as caller
            # — which won't match HTF length, so we either drop array-shaped ones or
            # carry scalars. Drop arrays to stay safe.
            if isinstance(v, np.ndarray):
                continue
            htf_context[k] = v
            carry_keys.add(k)

        sub_eval = Evaluator(
            htf_context, htf_n,
            symbol=self.symbol, timeframe=target_tf,
            htf_depth=self.htf_depth + 1,
        )
        # Share the call-count budget
        sub_eval.call_count = self.call_count
        sub_eval.start_time = self.start_time
        sub_result = sub_eval.eval(args[1])
        self.call_count = sub_eval.call_count

        # Get array form in HTF space
        if sub_result[0] == 'a':
            htf_arr = sub_result[1]
        elif sub_result[0] == 's':
            htf_arr = np.full(htf_n, float(sub_result[1]))
        else:
            raise ValueError("HTF sub-expression returned a non-numeric value")

        # Align HTF → caller via date-based forward-fill
        caller_dates = self.context.get('_dates')
        htf_dates = htf_context.get('_dates')
        if caller_dates is None or htf_dates is None:
            # No dates available — return NaN safely
            return ('a', _nan(self.n))

        try:
            caller_s = pd.Series(caller_dates).values.astype('datetime64[ns]')
            htf_s = pd.Series(htf_dates).values.astype('datetime64[ns]')
            s = pd.Series(htf_arr, index=htf_s)
            # Forward-fill HTF values onto caller's timeline
            out = s.reindex(pd.Index(caller_s).union(s.index).sort_values(), method=None)
            out = out.sort_index().ffill().reindex(caller_s).values
            return ('a', np.asarray(out, dtype=np.float64))
        except Exception as e:
            logger.debug(f"HTF alignment failed: {e}")
            return ('a', _nan(self.n))

    def _eval_divergence(self, args):
        """DIVERGENCE(price, osc, lookback=60) — regular bullish/bearish.

        Compares most recent pivot_high (bearish) or pivot_low (bullish) with
        the nearest prior pivot of the same kind within `lookback` bars.
        Uses left=right=5 for pivot detection.

        Returns:
          1.0 at bar confirming regular BEARISH divergence (price HH + osc LH)
         -1.0 at bar confirming regular BULLISH divergence (price LL + osc HL)
          0.0 elsewhere; NaN when insufficient data.
        """
        if len(args) < 2 or len(args) > 3:
            raise ValueError("DIVERGENCE expects (price, osc) or (price, osc, lookback)")
        price = self._to_arr(self.eval(args[0]))
        osc = self._to_arr(self.eval(args[1]))
        if len(args) == 3:
            lb_res = self.eval(args[2])
            if lb_res[0] != 's':
                raise ValueError("DIVERGENCE lookback must be a number")
            lookback = int(lb_res[1])
        else:
            lookback = 60

        if lookback < 20:
            raise ValueError("DIVERGENCE lookback must be >= 20")
        n = len(price)
        if n != len(osc):
            raise ValueError("DIVERGENCE: price and osc arrays must be same length")

        left = right = 5
        ph_price = _pivothigh(price, left, right)
        pl_price = _pivotlow(price, left, right)

        out = np.zeros(n)
        # Track indices of confirmed pivots
        price_highs = []  # list of (idx, price_val, osc_val_at_idx)
        price_lows = []
        for i in range(n):
            if not np.isnan(ph_price[i]):
                pivot_bar = i - right
                price_highs.append((pivot_bar, price[pivot_bar], osc[pivot_bar]))
            if not np.isnan(pl_price[i]):
                pivot_bar = i - right
                price_lows.append((pivot_bar, price[pivot_bar], osc[pivot_bar]))

            # Bearish: current confirmed pivot-high + prior pivot-high within lookback,
            # where price HH but osc LH.
            if not np.isnan(ph_price[i]) and len(price_highs) >= 2:
                cur_idx, cur_p, cur_o = price_highs[-1]
                prev_idx, prev_p, prev_o = price_highs[-2]
                if (cur_idx - prev_idx) <= lookback and not np.isnan(cur_o) and not np.isnan(prev_o):
                    if cur_p > prev_p and cur_o < prev_o:
                        out[i] = 1.0

            # Bullish: current confirmed pivot-low + prior, price LL but osc HL.
            if not np.isnan(pl_price[i]) and len(price_lows) >= 2:
                cur_idx, cur_p, cur_o = price_lows[-1]
                prev_idx, prev_p, prev_o = price_lows[-2]
                if (cur_idx - prev_idx) <= lookback and not np.isnan(cur_o) and not np.isnan(prev_o):
                    if cur_p < prev_p and cur_o > prev_o:
                        out[i] = -1.0

        # Mask early bars where pivots haven't had room to form
        if n > 0:
            warmup = min(n, left + right + 20)
            out[:warmup] = np.nan

        return ('a', out)

    def _offset_arr(self, arr, offset):
        n = len(arr)
        out = _nan(n)
        for i in range(n):
            j = i - offset
            if 0 <= j < n:
                out[i] = arr[j]
        return out


# ── Public API ─────────────────────────────────────────────────────────

def btc_reference_warnings(quant_deps: list, symbol: str) -> list:
    """Warnings for BTC-reference vars used on another symbol.

    Shared by evaluate_formula and backtest_indicator_formula so both
    surfaces flag the same thing the same way.
    """
    out = []
    sym = (symbol or '').upper()
    if sym in ('', 'BTC'):
        return out
    for var_name, series_name in quant_deps or []:
        if VAR_SCOPE.get(var_name) == 'btc_reference':
            out.append(
                f"{var_name.upper()} is a BTC-reference series "
                f"({series_name}); it does not track {sym}."
            )
    return out


def evaluate_formula(expression: str, symbol: str, timeframe: str = '1D') -> dict:
    """Evaluate a formula expression for a given symbol.

    Returns: {value: float|None, met: bool, evidence: str, error: str|None}
    """
    if not expression or not expression.strip():
        return {'value': None, 'met': False, 'evidence': '', 'error': 'Empty expression'}

    expression = expression.strip()
    if len(expression) > MAX_EXPRESSION_LENGTH:
        return {'value': None, 'met': False, 'evidence': '',
                'error': f'Expression too long (max {MAX_EXPRESSION_LENGTH} chars)'}

    # Load OHLCV
    ohlcv = load_ohlcv(symbol, timeframe)
    if ohlcv is None:
        return {'value': None, 'met': False,
                'evidence': f'No OHLCV data for {symbol}/{timeframe}', 'error': None}

    n = len(ohlcv['close'])
    if n == 0:
        return {'value': None, 'met': False, 'evidence': 'Empty data', 'error': None}

    # Build evaluation context
    context = dict(ohlcv)

    # Load quant dependencies if referenced
    quant_deps = get_quant_deps(expression)
    if quant_deps:
        _load_quant_into_context(context, quant_deps, n)

    # Load per-asset derivatives dependencies (scoped to this symbol)
    deriv_deps = get_deriv_deps(expression)
    if deriv_deps:
        try:
            _load_deriv_into_context(context, deriv_deps, symbol, n)
        except ValueError as e:
            return {'value': None, 'met': False, 'evidence': '', 'error': str(e)}

    # BTC-reference vars are single BTC-anchored series whose
    # names do not say so — surface that instead of silently substituting.
    warnings = btc_reference_warnings(quant_deps, symbol)

    try:
        tokens = tokenize(expression)
        parser = Parser(tokens)
        ast = parser.parse()
        evaluator = Evaluator(context, n, symbol=symbol, timeframe=timeframe)
        result = evaluator.eval(ast)

        # Get latest bar value
        if result[0] == 'a':
            arr = result[1]
            # Find last non-NaN value
            latest = np.nan
            for i in range(len(arr) - 1, -1, -1):
                if not np.isnan(arr[i]):
                    latest = arr[i]
                    break
        else:
            latest = result[1]

        if np.isnan(latest):
            out = {'value': None, 'met': False, 'evidence': 'Formula result is NaN', 'error': None}
            if warnings:
                out['warnings'] = warnings
            return out

        met = bool(latest > 0)
        # Truncate expression for evidence
        expr_short = expression[:60] + ('...' if len(expression) > 60 else '')
        evidence = f'{expr_short} = {latest:.4f}'

        out = {'value': float(latest), 'met': met, 'evidence': evidence, 'error': None}
        if warnings:
            out['warnings'] = warnings
        return out

    except (ValueError, TimeoutError) as e:
        return {'value': None, 'met': False, 'evidence': '', 'error': str(e)}
    except Exception as e:
        logger.warning(f"Formula eval error for '{expression}': {e}")
        return {'value': None, 'met': False, 'evidence': '', 'error': f'Evaluation error: {e}'}


def validate_formula(expression: str) -> dict:
    """Parse-only validation (no data needed).

    Returns: {valid: bool, error: str|None}
    """
    if not expression or not expression.strip():
        return {'valid': False, 'error': 'Empty expression'}

    expression = expression.strip()
    if len(expression) > MAX_EXPRESSION_LENGTH:
        return {'valid': False, 'error': f'Expression too long (max {MAX_EXPRESSION_LENGTH} chars)'}

    try:
        tokens = tokenize(expression)
        parser = Parser(tokens)
        parser.parse()
        return {'valid': True, 'error': None}
    except Exception as e:
        return {'valid': False, 'error': str(e)}
