"""Journal auto-sync engine.

Pulls read-only fills from a user's connected exchange (Binance spot or
Hyperliquid), aggregates them into position-level journal trades via a FIFO
builder, and writes them through the existing journal creation path
(``carrac.content_db.ContentDatabase``) so tier caps/analytics/MFE
hooks all apply the same way they do for manually-logged trades.

Design summary:
  - Idempotent: every raw fill is recorded in ``exchange_sync_fills`` keyed
    by (connection_id, exchange_trade_id) before it's fed to the FIFO
    builder; re-syncing an overlapping window is a no-op for fills already
    seen.
  - Incremental: each symbol's FIFO "still open" lot state + last-fill
    cursor is persisted in ``exchange_connections.cursor_data`` so a sync
    cycle only needs the fills newer than the last cursor, not full history.
  - The currently-open position for a symbol is tracked by the id of its
    live (``closed_at IS NULL``) trade_journal row — new fills update that
    SAME row (never create a second one) until it closes or flips.
  - Tier gate: this module assumes the caller (route / worker loop) has
    already confirmed ``limits.get('journal_autosync', False)`` for the
    connection's owner. Imports go straight through
    ``db.log_trade_open``/``log_trade_close`` (bypassing the MCP
    ``trade_log`` tool's monthly-cap check entirely) — sync is paid-only by
    construction, so the free-tier cap must never block it.
  - Dedupe: a closed (or newly-opening) position whose entry lands within
    ±30 minutes and ±10% size of an existing MANUAL trade on the same asset
    is NOT auto-imported — it's parked as a pending suggestion instead.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from carrac.db_pool import adapt_query, get_conn, handle_integrity_error
from carrac.journal.exchange_connections import (
    ensure_exchange_tables,
    get_decrypted_credentials,
    list_syncable_connections,
    mark_sync_error,
    mark_sync_success,
    update_cursor,
)
from carrac.journal.binance_auth_client import BinanceAuthClient
from carrac.journal.hyperliquid_fills_client import HyperliquidFillsClient

logger = logging.getLogger('carrac.journal.exchange_sync')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_unique_violation(exc) -> bool:
    try:
        return isinstance(exc, handle_integrity_error())
    except Exception:
        return False

_QTY_EPSILON = 1e-9

# Binance fills require a symbol parameter per request — cap the per-sync
# symbol universe so one connection can never fan out into an unbounded
# number of API calls.
_MAX_BINANCE_SYMBOLS = 30

# Dedupe guard vs. manually-logged trades.
_DEDUPE_WINDOW_MINUTES = 30
_DEDUPE_SIZE_TOLERANCE = 0.10


# ============================================================
# FIFO position builder (pure — no I/O, unit-tested directly)
# ============================================================

def fifo_consume(fills: list, resume: dict = None):
    """Walk ``fills`` (any order — sorted here by ``ts_ms``) and realize
    FIFO positions, optionally resuming a still-open lot from a previous run.

    Each fill is a dict: {id, symbol, side ('buy'|'sell'), price, qty, fee,
    fee_asset, ts_ms}. All fills must share one symbol — the caller groups
    by symbol before calling this.

    ``resume`` (optional): {'side': 'long'|'short', 'qty', 'avg_price',
    'fees', 'opened_at_ms'} — the open lot carried over from the last sync.

    Returns (closed, open_state):
      closed: list of fully-realized round trips (including flips), each
        {side, qty, entry_avg, exit_avg, fees, opened_at_ms, closed_at_ms}.
      open_state: dict shaped like ``resume``, or None if flat at the end.
    """
    ordered = sorted(fills, key=lambda f: f['ts_ms'])

    side = resume['side'] if resume else None
    qty = float(resume['qty']) if resume else 0.0
    avg_price = float(resume['avg_price']) if resume else 0.0
    fees = float(resume['fees']) if resume else 0.0
    opened_at_ms = resume['opened_at_ms'] if resume else None

    # Accumulator for a partial-close sequence (several opposite-side fills
    # closing the same lot before it fully flattens).
    exit_qty = 0.0
    exit_notional = 0.0
    exit_fees = 0.0

    closed = []

    for f in ordered:
        f_qty = float(f['qty'])
        f_price = float(f['price'])
        f_fee = float(f.get('fee') or 0.0)
        f_is_buy = f['side'] == 'buy'

        if qty <= _QTY_EPSILON:
            # Flat -> this fill opens a brand-new position.
            side = 'long' if f_is_buy else 'short'
            qty = f_qty
            avg_price = f_price
            fees = f_fee
            opened_at_ms = f['ts_ms']
            exit_qty = exit_notional = exit_fees = 0.0
            continue

        position_sign = 1.0 if side == 'long' else -1.0
        fill_sign = 1.0 if f_is_buy else -1.0
        same_direction = (fill_sign == position_sign)

        if same_direction:
            # Adds to the open lot — weighted-average entry.
            new_qty = qty + f_qty
            avg_price = (avg_price * qty + f_price * f_qty) / new_qty
            qty = new_qty
            fees += f_fee
            continue

        # Opposite direction — reduces (and possibly flips) the open lot.
        reduce_qty = min(f_qty, qty)
        reduce_fraction = (reduce_qty / f_qty) if f_qty else 0.0
        exit_qty += reduce_qty
        exit_notional += reduce_qty * f_price
        exit_fees += f_fee * reduce_fraction
        qty -= reduce_qty

        if qty <= _QTY_EPSILON:
            exit_avg = (exit_notional / exit_qty) if exit_qty else f_price
            closed.append({
                'side': side,
                'qty': round(exit_qty, 10),
                'entry_avg': avg_price,
                'exit_avg': exit_avg,
                'fees': round(fees + exit_fees, 10),
                'opened_at_ms': opened_at_ms,
                'closed_at_ms': f['ts_ms'],
            })

            leftover = f_qty - reduce_qty
            if leftover > _QTY_EPSILON:
                # Flip: the remainder of THIS fill opens the opposite position.
                leftover_fee = f_fee * (leftover / f_qty) if f_qty else 0.0
                side = 'long' if f_is_buy else 'short'
                qty = leftover
                avg_price = f_price
                fees = leftover_fee
                opened_at_ms = f['ts_ms']
            else:
                side = None
                qty = 0.0
                avg_price = 0.0
                fees = 0.0
                opened_at_ms = None

            exit_qty = exit_notional = exit_fees = 0.0
        # else: still open (partial close) — exit_* buffers keep accumulating
        # until a later fill fully flattens the lot.

    open_state = None
    if qty > _QTY_EPSILON:
        open_state = {
            'side': side, 'qty': qty, 'avg_price': avg_price,
            'fees': fees, 'opened_at_ms': opened_at_ms,
        }
    return closed, open_state


# ============================================================
# Fill normalization
# ============================================================

def normalize_binance_fill(raw: dict) -> dict:
    """Binance myTrades row -> the common fill shape."""
    return {
        'id': str(raw['id']),
        'symbol': raw['symbol'],
        'side': 'buy' if raw.get('isBuyer') else 'sell',
        'price': float(raw['price']),
        'qty': float(raw['qty']),
        'fee': float(raw.get('commission') or 0),
        'fee_asset': raw.get('commissionAsset') or '',
        'ts_ms': int(raw['time']),
    }


def normalize_hl_fill(raw: dict) -> dict:
    """Hyperliquid userFills row -> the common fill shape.

    ``tid`` is HL's unique fill id; some historical rows may lack it, so we
    fall back to a (oid, time) composite — good enough for idempotency
    since a genuine duplicate always carries the same oid+time pair.
    """
    tid = raw.get('tid') or raw.get('hash') or f"{raw.get('oid')}-{raw.get('time')}"
    return {
        'id': str(tid),
        'symbol': raw['coin'],
        'side': 'buy' if raw.get('side') == 'B' else 'sell',
        'price': float(raw['px']),
        'qty': float(raw['sz']),
        'fee': float(raw.get('fee') or 0),
        'fee_asset': raw.get('feeToken') or 'USDC',
        'ts_ms': int(raw['time']),
    }


def _to_binance_pair(asset: str) -> str:
    from data_sources.binance import BinanceClient
    upper = asset.upper()
    mapped = BinanceClient.SYMBOL_MAP.get(upper, upper)
    return f'{mapped}USDT'


def _asset_from_symbol(exchange: str, symbol: str) -> str:
    if exchange == 'binance' and symbol.upper().endswith('USDT'):
        return symbol.upper()[:-4]
    return symbol.upper()


def _binance_symbol_universe(db, user_id: int) -> list:
    """Union of watchlist + existing-journal-trade + open-position symbols,
    capped at ``_MAX_BINANCE_SYMBOLS`` (logs what got skipped)."""
    from user_db import user_db as _user_db

    assets = set()
    try:
        assets.update(_user_db.get_flat_watchlist(user_id) or [])
    except Exception:
        logger.debug('exchange_sync: watchlist lookup failed for user %s', user_id, exc_info=True)
    try:
        assets.update(t['asset'] for t in db.get_open_trades(user_id) if t.get('asset'))
    except Exception:
        logger.debug('exchange_sync: open-trades lookup failed for user %s', user_id, exc_info=True)
    try:
        assets.update(
            t['asset'] for t in db.get_trade_history(user_id, days=90, limit=200) if t.get('asset')
        )
    except Exception:
        logger.debug('exchange_sync: trade-history lookup failed for user %s', user_id, exc_info=True)

    pairs = []
    for a in sorted(assets):
        pair = _to_binance_pair(a)
        if pair not in pairs:
            pairs.append(pair)

    if len(pairs) > _MAX_BINANCE_SYMBOLS:
        skipped = pairs[_MAX_BINANCE_SYMBOLS:]
        logger.info(
            'exchange_sync: user %s symbol universe capped at %s; skipped %s',
            user_id, _MAX_BINANCE_SYMBOLS, skipped,
        )
        pairs = pairs[:_MAX_BINANCE_SYMBOLS]
    return pairs


# ============================================================
# Raw-fill idempotency ledger
# ============================================================

def _filter_new_fills(connection_id: int, fills: list) -> list:
    """Drop any fill already recorded for this connection (dedupe safety net —
    independent of cursor correctness: a retried/overlapping fetch window
    can hand back fills we've already processed)."""
    if not fills:
        return []
    ids = [f['id'] for f in fills]
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        placeholders = ','.join(['?'] * len(ids))
        rows = conn.execute(
            adapt_query(
                f'SELECT exchange_trade_id FROM exchange_sync_fills '
                f'WHERE connection_id = ? AND exchange_trade_id IN ({placeholders})'
            ),
            [connection_id] + ids,
        ).fetchall()
        known = {(r['exchange_trade_id'] if isinstance(r, dict) else r[0]) for r in rows}
    return [f for f in fills if f['id'] not in known]


def _record_fills(connection_id: int, symbol: str, fills: list) -> None:
    if not fills:
        return
    now = _now_iso()
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        for f in fills:
            try:
                conn.execute(
                    adapt_query('''INSERT INTO exchange_sync_fills
                       (connection_id, exchange_trade_id, symbol, fill_time, created_at)
                       VALUES (?, ?, ?, ?, ?)'''),
                    (
                        connection_id, f['id'], symbol,
                        datetime.fromtimestamp(f['ts_ms'] / 1000, tz=timezone.utc).isoformat(),
                        now,
                    ),
                )
            except Exception as exc:
                if _is_unique_violation(exc):
                    continue  # already recorded (race with a concurrent call) — fine
                raise


# ============================================================
# Dedupe-vs-manual-entry suggestions
# ============================================================

def _find_manual_dupe(db, user_id: int, asset: str, entry_dt: datetime, size) -> int:
    """Return the id of a matching manual trade, or None.

    Match window: opened_at within ±30 min. If both sides have a recorded
    size, it must also be within ±10%; if the manual trade has no size on
    file, timestamp proximity alone is treated as a match (conservative
    toward flagging rather than silently importing a possible duplicate).
    """
    start = (entry_dt - timedelta(minutes=_DEDUPE_WINDOW_MINUTES)).isoformat()
    end = (entry_dt + timedelta(minutes=_DEDUPE_WINDOW_MINUTES)).isoformat()
    candidates = db.find_candidate_manual_trades(user_id, asset, start, end)
    for c in candidates:
        c_size = c.get('size')
        if c_size is None or size is None or size == 0:
            return c['id']
        if abs(c_size - size) / size <= _DEDUPE_SIZE_TOLERANCE:
            return c['id']
    return None


def create_suggestion(*, connection_id, user_id, exchange, symbol, direction,
                       entry_price, exit_price, size, fees, opened_at, closed_at,
                       matched_trade_id) -> None:
    now = _now_iso()
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        conn.execute(
            adapt_query('''INSERT INTO exchange_import_suggestions
                (connection_id, user_id, exchange, symbol, direction, entry_price, exit_price,
                 size, fees, opened_at, closed_at, matched_trade_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)'''),
            (
                connection_id, user_id, exchange, symbol, direction, entry_price, exit_price,
                size, fees, opened_at.isoformat(), closed_at.isoformat() if closed_at else None,
                matched_trade_id, now,
            ),
        )


def list_suggestions(user_id: int, status: str = 'pending') -> list:
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        if status:
            rows = conn.execute(
                adapt_query(
                    'SELECT * FROM exchange_import_suggestions '
                    'WHERE user_id = ? AND status = ? ORDER BY created_at DESC'
                ),
                (user_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                adapt_query(
                    'SELECT * FROM exchange_import_suggestions WHERE user_id = ? ORDER BY created_at DESC'
                ),
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_suggestion(suggestion_id: int, user_id: int) -> dict:
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        row = conn.execute(
            adapt_query('SELECT * FROM exchange_import_suggestions WHERE id = ? AND user_id = ?'),
            (suggestion_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def _update_suggestion_status(suggestion_id: int, status: str, journal_trade_id: int = None) -> None:
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        conn.execute(
            adapt_query('''UPDATE exchange_import_suggestions
                SET status = ?, resolved_at = ?, journal_trade_id = COALESCE(?, journal_trade_id)
                WHERE id = ?'''),
            (status, _now_iso(), journal_trade_id, suggestion_id),
        )


def resolve_suggestion(suggestion_id: int, user_id: int, action: str) -> dict:
    """action: 'import' materializes the suggestion as a real journal trade
    (tagged src:<exchange>); 'skip' just marks it resolved. Returns the
    updated suggestion dict, or None if not found/not owned."""
    from carrac.content_db import get_content_db

    sugg = get_suggestion(suggestion_id, user_id)
    if not sugg:
        return None
    if sugg['status'] != 'pending':
        return sugg

    if action == 'skip':
        _update_suggestion_status(suggestion_id, 'skipped')
        sugg['status'] = 'skipped'
        return sugg
    if action != 'import':
        raise ValueError('action must be "import" or "skip"')

    db = get_content_db()
    tags = json.dumps([f"src:{sugg['exchange']}"])
    trade_id = db.log_trade_open(
        user_id=user_id, asset=sugg['symbol'], timeframe='1D',
        direction=sugg['direction'], entry_price=sugg['entry_price'], tags=tags,
    )
    updates = {}
    if sugg.get('size') is not None:
        updates['size'] = sugg['size']
    if sugg.get('fees') is not None:
        updates['fees'] = sugg['fees']
    if updates:
        db.trade_update(trade_id, **updates)
    if sugg.get('exit_price') is not None:
        db.log_trade_close(trade_id, exit_price=sugg['exit_price'], exit_reason='manual')

    _update_suggestion_status(suggestion_id, 'imported', journal_trade_id=trade_id)
    sugg['status'] = 'imported'
    sugg['journal_trade_id'] = trade_id
    return sugg


# ============================================================
# Materializing FIFO output into journal trades
# ============================================================

def _materialize_closed_position(db, user_id, connection_id, exchange, asset, pos,
                                  existing_open_trade_id=None):
    """Returns (imported_count, suggested_count) — always 1 and 0, or 0 and 1."""
    direction = 'long' if pos['side'] == 'long' else 'short'
    entry_dt = datetime.fromtimestamp(pos['opened_at_ms'] / 1000, tz=timezone.utc)
    closed_dt = datetime.fromtimestamp(pos['closed_at_ms'] / 1000, tz=timezone.utc)

    dupe_id = _find_manual_dupe(db, user_id, asset, entry_dt, pos['qty'])
    if dupe_id:
        create_suggestion(
            connection_id=connection_id, user_id=user_id, exchange=exchange, symbol=asset,
            direction=direction, entry_price=pos['entry_avg'], exit_price=pos['exit_avg'],
            size=pos['qty'], fees=pos['fees'], opened_at=entry_dt, closed_at=closed_dt,
            matched_trade_id=dupe_id,
        )
        return 0, 1

    tags = json.dumps([f'src:{exchange}'])
    if existing_open_trade_id:
        db.trade_update(existing_open_trade_id, entry_price=pos['entry_avg'],
                        size=pos['qty'], fees=pos['fees'])
        db.log_trade_close(existing_open_trade_id, exit_price=pos['exit_avg'], exit_reason='manual')
    else:
        trade_id = db.log_trade_open(
            user_id=user_id, asset=asset, timeframe='1D', direction=direction,
            entry_price=pos['entry_avg'], tags=tags,
        )
        db.trade_update(trade_id, size=pos['qty'], fees=pos['fees'])
        db.log_trade_close(trade_id, exit_price=pos['exit_avg'], exit_reason='manual')
    return 1, 0


def _materialize_open_position(db, user_id, connection_id, exchange, asset, open_state,
                                existing_open_trade_id=None):
    """Returns (trade_id_or_None, imported_count, suggested_count).

    ``trade_id`` is None when the open position was parked as a dedupe
    suggestion instead of created — the caller must not persist a cursor
    open_trade_id in that case.
    """
    direction = 'long' if open_state['side'] == 'long' else 'short'

    if existing_open_trade_id:
        db.trade_update(existing_open_trade_id, entry_price=open_state['avg_price'],
                        size=open_state['qty'], fees=open_state['fees'])
        return existing_open_trade_id, 0, 0

    entry_dt = datetime.fromtimestamp(open_state['opened_at_ms'] / 1000, tz=timezone.utc)
    dupe_id = _find_manual_dupe(db, user_id, asset, entry_dt, open_state['qty'])
    if dupe_id:
        create_suggestion(
            connection_id=connection_id, user_id=user_id, exchange=exchange, symbol=asset,
            direction=direction, entry_price=open_state['avg_price'], exit_price=None,
            size=open_state['qty'], fees=open_state['fees'], opened_at=entry_dt, closed_at=None,
            matched_trade_id=dupe_id,
        )
        return None, 0, 1

    tags = json.dumps([f'src:{exchange}'])
    trade_id = db.log_trade_open(
        user_id=user_id, asset=asset, timeframe='1D', direction=direction,
        entry_price=open_state['avg_price'], tags=tags,
    )
    db.trade_update(trade_id, size=open_state['qty'], fees=open_state['fees'])
    return trade_id, 1, 0


# ============================================================
# Per-connection sync
# ============================================================

def sync_connection(connection: dict, *, db=None) -> dict:
    """Sync one exchange connection. Raises on hard failures (auth error,
    network error) so the caller records it against the connection's
    consecutive-error counter; never raises for "nothing new"."""
    from carrac.content_db import get_content_db

    db = db or get_content_db()
    creds = get_decrypted_credentials(connection)
    exchange = connection['exchange']
    user_id = connection['user_id']
    connection_id = connection['id']
    cursor = json.loads(connection.get('cursor_data') or '{}')

    fills_by_symbol = {}

    if exchange == 'binance':
        client = BinanceAuthClient(creds['api_key'], creds['api_secret'])
        for pair in _binance_symbol_universe(db, user_id):
            sym_cursor = cursor.get(pair) or {}
            start_ms = sym_cursor.get('last_fill_ts_ms')
            try:
                raw = client.get_spot_fills(pair, start_time_ms=start_ms)
            except Exception as exc:
                # A single unknown/delisted symbol must not sink the whole
                # connection's sync pass.
                logger.debug('exchange_sync: %s fetch failed for %s: %s', pair, connection_id, exc)
                continue
            if raw:
                fills_by_symbol[pair] = [normalize_binance_fill(r) for r in raw]

    elif exchange == 'hyperliquid':
        client = HyperliquidFillsClient(creds['address'])
        hl_cursor = cursor.get('_hl') or {}
        start_ms = hl_cursor.get('last_fill_ts_ms')
        raw = client.get_fills(start_time_ms=start_ms)
        for r in raw:
            f = normalize_hl_fill(r)
            fills_by_symbol.setdefault(f['symbol'], []).append(f)
    else:
        raise ValueError(f'Unsupported exchange: {exchange}')

    total_imported = 0
    total_suggested = 0
    new_cursor = dict(cursor)

    for symbol, fills in fills_by_symbol.items():
        cursor_key = symbol if exchange == 'binance' else f'hl:{symbol}'
        sym_cursor = cursor.get(cursor_key) or {}

        new_fills = _filter_new_fills(connection_id, fills)
        if not new_fills:
            continue
        _record_fills(connection_id, symbol, new_fills)

        resume = None
        if sym_cursor.get('open_trade_id'):
            resume = {
                'side': sym_cursor['open_side'], 'qty': sym_cursor['open_qty'],
                'avg_price': sym_cursor['open_avg_price'], 'fees': sym_cursor['open_fees'],
                'opened_at_ms': sym_cursor['opened_at_ms'],
            }

        closed, open_state = fifo_consume(new_fills, resume=resume)
        asset = _asset_from_symbol(exchange, symbol)

        for i, pos in enumerate(closed):
            existing_id = sym_cursor.get('open_trade_id') if i == 0 else None
            imported, suggested = _materialize_closed_position(
                db, user_id, connection_id, exchange, asset, pos,
                existing_open_trade_id=existing_id,
            )
            total_imported += imported
            total_suggested += suggested

        last_ts = new_fills[-1]['ts_ms']
        if open_state is not None:
            reuse_id = sym_cursor.get('open_trade_id') if not closed else None
            trade_id, imported, suggested = _materialize_open_position(
                db, user_id, connection_id, exchange, asset, open_state,
                existing_open_trade_id=reuse_id,
            )
            total_imported += imported
            total_suggested += suggested
            new_cursor[cursor_key] = {
                'open_trade_id': trade_id,
                'open_side': open_state['side'], 'open_qty': open_state['qty'],
                'open_avg_price': open_state['avg_price'], 'open_fees': open_state['fees'],
                'opened_at_ms': open_state['opened_at_ms'],
                'last_fill_ts_ms': last_ts,
            }
        else:
            new_cursor[cursor_key] = {'open_trade_id': None, 'last_fill_ts_ms': last_ts}

    update_cursor(connection_id, new_cursor)
    mark_sync_success(connection_id, total_imported)
    return {'imported': total_imported, 'suggested': total_suggested}


# ============================================================
# Worker entry point — periodic pass over ALL connections
# ============================================================

def sync_all_connections() -> dict:
    """One pass over every syncable connection, called every 15 minutes by
    the worker's exchange-sync scheduler (see worker.py).

    Per-connection error isolation: one bad key/credential/network blip
    never aborts the loop, and never touches another user's connection.
    Re-checks the owner's CURRENT tier on every pass (not just at connect
    time) so a downgrade silently pauses syncing instead of erroring.
    """
    from user_db import user_db as _user_db
    from carrac.auth.decorators import get_tier_limits_for_user

    connections = list_syncable_connections()
    synced = skipped_tier = errored = 0

    for conn_row in connections:
        connection_id = conn_row['id']
        user_id = conn_row['user_id']

        try:
            user_row = _user_db.get_user_by_id(user_id)
            tier = (user_row or {}).get('tier') or 'free'
            limits = get_tier_limits_for_user(user_id, tier)
        except Exception:
            logger.debug('exchange_sync: tier lookup failed for user %s', user_id, exc_info=True)
            limits = {}

        if not limits.get('journal_autosync', False):
            skipped_tier += 1
            logger.debug(
                'exchange_sync: user %s tier lacks journal_autosync — skipping connection %s',
                user_id, connection_id,
            )
            continue

        try:
            sync_connection(conn_row)
            synced += 1
        except Exception as exc:
            errored += 1
            new_count, status = mark_sync_error(connection_id, str(exc))
            logger.warning(
                'exchange_sync: connection %s (%s) failed (%s consecutive) — %s',
                connection_id, conn_row.get('exchange'), new_count, exc,
            )
            if status == 'error':
                logger.error(
                    'exchange_sync: connection %s disabled after %s consecutive failures — '
                    'user must re-save credentials', connection_id, new_count,
                )
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(exc)
            except Exception:
                pass

    if synced or errored or skipped_tier:
        logger.info(
            'exchange_sync: pass complete — synced=%s skipped_tier=%s errored=%s',
            synced, skipped_tier, errored,
        )
    return {'synced': synced, 'skipped_tier': skipped_tier, 'errored': errored}
