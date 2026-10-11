"""
MFE/MAE (Max Favorable / Adverse Excursion) calculator.

Supports two modes:

**Closed trade** (``trade.closed_at`` is set):
    Window = entry_timestamp − 5 min → close_timestamp + 5 min.
    Requires both ``entry_price`` and ``exit_price``.
    ``exit_efficiency`` is computed (how much of MFE was captured).
    Result carries ``is_live: False``.

**Open trade** (``trade.closed_at`` is NULL):
    Window = entry_timestamp − 5 min → utcnow() + 5 min.
    Requires ``entry_price`` only; ``exit_price`` is not needed.
    ``exit_efficiency`` is left NULL (no exit to evaluate).
    Result carries ``is_live: True`` — callers should treat this as a
    rolling snapshot, not a final record.

Bar interval selection is identical for both paths and is driven by the
window duration (not a hardcoded constant):
  < 4 h  → 1 m
  < 48 h → 5 m
  < 168 h (1 week) → 15 m
  ≥ 168 h → 1 h

Data source: ``OHLCVDatabase.fetch_klines_for_range`` (Binance →
Hyperliquid → CCXT fallback chain) replaces the previous
``fetch_binance_klines_for_range``-only call so that Hyperliquid and
CCXT-native symbols are covered for both open and closed trades.
"""

import logging
from datetime import datetime

logger = logging.getLogger('carrac.journal.mfe_mae')


def calculate_and_store_mfe_mae(db, journal_id: int) -> dict:
    """Calculate MFE/MAE for a trade (open or closed) and persist the results.

    For open trades the window ends at ``datetime.utcnow()`` and
    ``exit_efficiency`` is not computed.  For closed trades the original
    behaviour is preserved and ``exit_efficiency`` is computed from
    ``rr_actual``.

    Args:
        db: ContentDatabase instance
        journal_id: trade_journal.id

    Returns:
        dict with keys:
          mfe_price, mae_price, mfe_r (or None), mae_r (or None),
          exit_efficiency (closed only), interval_used, bars_analyzed,
          is_live (True = open trade snapshot, False = closed trade final).
        On error returns ``{'error': '<reason>'}`` — callers must check for
        this key before treating the result as valid data.
    """
    trade = db.get_journal_entry(journal_id)
    if not trade:
        return {'error': 'Trade not found'}

    is_open = not trade.get('closed_at')

    if not trade.get('entry_price'):
        return {'error': 'Missing entry price'}

    if not is_open and not trade.get('exit_price'):
        return {'error': 'Missing exit price'}

    entry_price = trade['entry_price']
    stop_price = trade.get('stop_price')
    direction = trade.get('direction', 'long')

    # Parse timestamps
    try:
        opened = _parse_timestamp(trade['opened_at'])
    except Exception as e:
        logger.warning(f"Invalid opened_at for trade {journal_id}: {e}")
        return {'error': 'Invalid trade timestamps'}

    if is_open:
        window_end = datetime.utcnow()
    else:
        try:
            window_end = _parse_timestamp(trade['closed_at'])
        except Exception as e:
            logger.warning(f"Invalid closed_at for trade {journal_id}: {e}")
            return {'error': 'Invalid trade timestamps'}

    # Add small buffer (5 min before open, 5 min after close/now)
    start_ms = int(opened.timestamp() * 1000) - 300_000
    end_ms = int(window_end.timestamp() * 1000) + 300_000

    # Choose interval based on trade duration
    duration_hours = (window_end - opened).total_seconds() / 3600
    if duration_hours < 4:
        interval = '1m'
    elif duration_hours < 48:
        interval = '5m'
    elif duration_hours < 168:  # 1 week
        interval = '15m'
    else:
        interval = '1h'

    # Fetch bars via venue-aware chain (Binance → Hyperliquid → CCXT)
    try:
        from database import OHLCVDatabase
        ohlcv_db = OHLCVDatabase()
        df = ohlcv_db.fetch_klines_for_range(
            symbol=trade['asset'],
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    except Exception as e:
        logger.warning(f"Failed to fetch bars for MFE/MAE (trade {journal_id}): {e}")
        return {'error': 'Failed to fetch price data for trade period'}

    if df is None or df.empty:
        return {'error': 'No price data available for trade period'}

    # Calculate MFE/MAE
    if direction == 'long':
        mfe_price = float(df['high'].max())
        mae_price = float(df['low'].min())
    else:  # short
        mfe_price = float(df['low'].min())
        mae_price = float(df['high'].max())

    # Calculate in R units if stop is available
    risk = abs(entry_price - stop_price) if stop_price and stop_price != entry_price else None

    if direction == 'long':
        mfe_raw = mfe_price - entry_price
        mae_raw = entry_price - mae_price
    else:
        mfe_raw = entry_price - mfe_price
        mae_raw = mae_price - entry_price

    mfe_r = round(mfe_raw / risk, 2) if risk else None
    mae_r = round(mae_raw / risk, 2) if risk else None

    # Exit efficiency: only meaningful for closed trades (needs rr_actual)
    exit_efficiency = None
    if not is_open:
        rr_actual = trade.get('rr_actual')
        if mfe_r and mfe_r > 0 and rr_actual is not None:
            exit_efficiency = round(max(0, rr_actual) / mfe_r, 2)

    # Store results
    updates = {
        'mfe_price': round(mfe_price, 8),
        'mae_price': round(mae_price, 8),
        'mfe_computed_at': datetime.utcnow().isoformat(),
    }
    if mfe_r is not None:
        updates['mfe'] = mfe_r
    if mae_r is not None:
        updates['mae'] = mae_r
    if exit_efficiency is not None:
        updates['exit_efficiency'] = exit_efficiency

    db.trade_update(journal_id, **updates)

    result = {
        'mfe_price': updates['mfe_price'],
        'mae_price': updates['mae_price'],
        'mfe_r': mfe_r,
        'mae_r': mae_r,
        'exit_efficiency': exit_efficiency,
        'interval_used': interval,
        'bars_analyzed': len(df),
        'is_live': is_open,
    }
    live_tag = ' [ROLLING]' if is_open else ''
    logger.info(
        f"MFE/MAE calculated for trade {journal_id}{live_tag}: "
        f"MFE={mfe_r}R, MAE={mae_r}R, efficiency={exit_efficiency}"
    )
    return result


def _parse_timestamp(ts) -> datetime:
    """Parse a trade timestamp to a NAIVE datetime, handling various formats.

    ``db.get_journal_entry`` returns TIMESTAMP columns as ``datetime`` objects
    on Postgres (psycopg) but as ISO strings on SQLite — accept both. Timezone
    info is stripped so arithmetic stays consistent with ``datetime.utcnow()``.
    """
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None)
    # ISO string path — strip tz/fractional for a naive comparison
    clean = str(ts).replace('Z', '').split('+')[0].split('.')[0]
    return datetime.fromisoformat(clean)
