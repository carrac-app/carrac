"""MCP tool: util_resolve_timestamp — bar snapping precision helper.

Converts human-readable dates / relative offsets to bar-aligned epoch ms
so callers can create drawings, filters, and queries with exact bar boundaries
without manually computing Binance bar timestamps.
"""

import logging

import pandas as pd

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError

logger = logging.getLogger('carrac.mcp.timestamps')

# Generous bar-count windows for date snapping.  Scaled per timeframe so the
# target date is virtually always within the fetched window.
_BARS_NEEDED = {
    '1D': 400, '1W': 200, '1M': 60, '3D': 150,
    '4H': 2400, '4h': 2400,
    '1H': 9000, '1h': 9000,
    '15M': 35000, '15m': 35000,
    '5M': 35000, '5m': 35000,
    '1min': 35000, '1m': 35000,
}

# Interval durations in milliseconds for computing bar_close_iso.
_INTERVAL_MS = {
    '1M': 30 * 24 * 3600 * 1000,   # approx
    '1W': 7 * 24 * 3600 * 1000,
    '3D': 3 * 24 * 3600 * 1000,
    '1D': 24 * 3600 * 1000,
    '4h': 4 * 3600 * 1000,
    '1h': 3600 * 1000,
    '15m': 15 * 60 * 1000,
    '5m': 5 * 60 * 1000,
    '1m': 60 * 1000,
}


@mcp_tool(
    'util_resolve_timestamp',
    'Resolve a human-readable date or relative bar offset to the bar-aligned '
    'epoch ms timestamp used by KLineChart drawings. '
    'Pass an ISO 8601 date ("2026-05-01" or "2026-05-01T16:00:00Z"), '
    '"now"/"latest" for the most recent bar, or "-N" (e.g. "-5") for N bars '
    'before the latest. Returns epoch ms, ISO open/close times, and bar index '
    'from the end (0 = latest). Use the returned epoch_ms directly in '
    'drawing_create from_ts/to_ts fields for pixel-perfect placement.',
    {
        'type': 'object',
        'properties': {
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol, e.g. BTC, ETH',
            },
            'timeframe': {
                'type': 'string',
                'description': 'Chart timeframe, e.g. 1D, 4h, 1h, 15m, 1W',
            },
            'when': {
                'type': 'string',
                'description': (
                    'When to resolve. One of: '
                    'ISO 8601 date/datetime ("2026-05-01", "2026-05-01T16:00:00Z"); '
                    '"now" or "latest" for the most recent bar; '
                    '"-N" where N is a non-negative integer for N bars before latest '
                    '(e.g. "-0" = latest, "-5" = 5 bars ago).'
                ),
            },
        },
        'required': ['symbol', 'timeframe', 'when'],
    },
    min_tier='free',
)
def util_resolve_timestamp(arguments):
    from database import OHLCVDatabase

    symbol = (arguments.get('symbol') or '').strip().upper()
    timeframe = (arguments.get('timeframe') or '').strip()
    when = (arguments.get('when') or '').strip()

    if not symbol:
        raise ToolError('symbol is required')
    if not timeframe:
        raise ToolError('timeframe is required')
    if not when:
        raise ToolError('when is required')

    db = OHLCVDatabase()
    bars_needed = _BARS_NEEDED.get(timeframe, _BARS_NEEDED.get(timeframe.lower(), 1000))
    is_clamped = False

    # Map the user-facing timeframe to the right cached accessor on the DB.
    # The chart endpoints prefer cached multi-tf data because direct Binance
    # fetches occasionally fail (rate limit / geo block) — the cache is the
    # canonical bar source the rest of the app uses.
    _HTF_KEYS = {'1D', '1d', '3D', '3d', '1W', '1w', '1M'}
    _LTF_KEYS = {'15m', '15M', '1h', '1H', '4h', '4H'}

    def _ensure_dt_index(d):
        """Coerce df.index to UTC DatetimeIndex.

        Handles three cases the venue / cache layers produce:
          1. integer ms epoch  → pd.to_datetime(unit='ms', utc=True)
          2. tz-naive datetime → tz_localize('UTC')
          3. tz-aware datetime → leave alone
        """
        if d is None or d.empty:
            return d
        if not pd.api.types.is_datetime64_any_dtype(d.index):
            d.index = pd.to_datetime(d.index, unit='ms', utc=True)
        elif d.index.tz is None:
            d.index = d.index.tz_localize('UTC')
        return d

    def _fetch(bars):
        """Best-effort fetch — prefers cached multi-tf data, falls back to live."""
        # HTF: use cached multi_timeframe_data
        if timeframe in _HTF_KEYS or timeframe.upper() in {'1D', '3D', '1W', '1M'}:
            try:
                mt = db.get_multi_timeframe_data(symbol)
                key = timeframe.upper() if timeframe.upper() in mt else (
                    '1D' if timeframe.lower() == '1d' else timeframe
                )
                if key in mt and mt[key] is not None and not mt[key].empty:
                    df = mt[key].tail(bars).copy()
                    return _ensure_dt_index(df)
            except Exception as exc:
                logger.debug('HTF cached fetch failed (%s/%s): %s', symbol, timeframe, exc)
        # LTF: use cached ltf_multi_timeframe_data when available
        if timeframe in _LTF_KEYS:
            try:
                ltf = db.get_ltf_multi_timeframe_data(symbol)
                key = timeframe if timeframe in ltf else timeframe.lower()
                if key in ltf and ltf[key] is not None and not ltf[key].empty:
                    df = ltf[key].tail(bars).copy()
                    return _ensure_dt_index(df)
            except Exception as exc:
                logger.debug('LTF cached fetch failed (%s/%s): %s', symbol, timeframe, exc)
        # Fallback: live venue-aware fetch
        return _ensure_dt_index(db.fetch_klines_for_chart(symbol, interval=timeframe, bars=bars))

    try:
        if when in ('now', 'latest'):
            df = _fetch(2)
            if df is None or df.empty:
                raise ToolError(f'No kline data for {symbol}/{timeframe}')
            bar = df.index[-1]
            bar_index_from_end = 0

        elif when.startswith('-') and when[1:].isdigit():
            n = int(when[1:])
            fetch_bars = n + 2
            df = _fetch(fetch_bars)
            if df is None or df.empty:
                raise ToolError(f'No kline data for {symbol}/{timeframe}')
            if n >= len(df):
                raise ToolError(
                    f'Requested -{n} bars ago but only {len(df)} bars available '
                    f'for {symbol}/{timeframe}. Try a smaller N.'
                )
            idx = len(df) - 1 - n
            bar = df.index[idx]
            bar_index_from_end = -n

        else:
            # ISO date / datetime
            try:
                if 'T' in when or '+' in when or 'Z' in when:
                    target = pd.Timestamp(when, tz='UTC')
                else:
                    target = pd.Timestamp(when + 'T00:00:00', tz='UTC')
            except Exception:
                raise ToolError(
                    f'Cannot parse when="{when}". Use ISO 8601 ("2026-05-01"), '
                    '"now", or "-N" (bars ago).'
                )

            df = _fetch(bars_needed)
            if df is None or df.empty:
                raise ToolError(f'No kline data for {symbol}/{timeframe}')

            if target < df.index[0]:
                raise ToolError(
                    f'Target {when} is before earliest cached bar '
                    f'({df.index[0].isoformat()}) for {symbol}/{timeframe}. '
                    f'Pick a later date or use a higher timeframe.'
                )

            bar_ts = df.index.asof(target)
            if pd.isna(bar_ts):
                # target is after latest bar — clamp to latest
                bar = df.index[-1]
                is_clamped = True
                bar_index_from_end = 0
            else:
                bar = bar_ts
                # Compute bar_index_from_end: position from the end
                pos = df.index.get_loc(bar)
                bar_index_from_end = pos - (len(df) - 1)  # <= 0

    except ToolError:
        raise
    except Exception as exc:
        logger.error('util_resolve_timestamp error for %s/%s when=%s: %s', symbol, timeframe, when, exc)
        raise ToolError(f'Failed to resolve timestamp: {exc}')

    epoch_ms = int(bar.timestamp() * 1000)
    epoch_s = epoch_ms // 1000

    # Compute bar_close_iso: bar open + interval duration
    interval_ms = _INTERVAL_MS.get(timeframe, _INTERVAL_MS.get(timeframe.lower(), 0))
    if interval_ms:
        close_ts = pd.Timestamp(epoch_ms + interval_ms, unit='ms', tz='UTC')
        bar_close_iso = close_ts.isoformat()
    else:
        bar_close_iso = None  # unknown interval — caller can infer

    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'epoch_ms': epoch_ms,
        'epoch_s': epoch_s,
        'bar_open_iso': bar.isoformat(),
        'bar_close_iso': bar_close_iso,
        'is_clamped': is_clamped,
        'bar_index_from_end': bar_index_from_end,
    }
