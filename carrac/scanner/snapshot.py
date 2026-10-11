"""
Capture the scanner score at trade open and close, for the journal.

Best-effort: logs warning on failure, never raises.
"""

import json
import logging

logger = logging.getLogger('carrac.scanner.snapshot')


def capture_score_snapshot(db, user_id, journal_id, asset, direction,
                           snapshot_type='entry', trade_levels=None):
    """Capture full indicator + score snapshot at current moment.

    Args:
        db: ContentDatabase instance
        user_id: user ID
        journal_id: trade_journal.id
        asset: symbol string (e.g. 'BTC')
        direction: 'long' or 'short'
        snapshot_type: 'entry' or 'exit'
        trade_levels: dict with entry_price, stop_price, tp1_price (optional)
    """
    try:
        _do_capture(db, user_id, journal_id, asset, direction,
                    snapshot_type, trade_levels)
    except Exception:
        # logger.exception (not .warning) so the traceback is recorded and
        # promoted to a first-class Sentry event — a prior larsson_*/
        # ema_ribbon_* column mismatch made every production INSERT fail here for
        # months with nothing but a WARNING string in the logs.
        logger.exception(f"Snapshot capture failed ({snapshot_type} {asset})")


def _do_capture(db, user_id, journal_id, asset, direction,
                snapshot_type, trade_levels):
    """Internal capture logic — may raise."""
    from carrac.precompute import response_cache
    from carrac.scanner.scorer import score_asset, _build_macro_data

    # Get latest HTF data from cache
    cached = response_cache.get('htf')
    if not cached:
        logger.warning("No HTF cache available for snapshot")
        return

    # Find asset data
    asset_data = None
    for a in cached.get('assets', []):
        if a.get('symbol') == asset:
            asset_data = a
            break

    if not asset_data:
        logger.warning(f"Asset {asset} not found in HTF cache for snapshot")
        return

    # Score the asset
    macro_data = _build_macro_data()
    score_result = score_asset(asset_data, macro_data, direction)

    # Extract raw indicator values
    consensio = asset_data.get('consensio') or {}
    ema_ribbon = asset_data.get('ema_ribbon') or {}
    macd = asset_data.get('macd') or {}
    structure = asset_data.get('structure') or {}
    cpr = asset_data.get('cpr') or {}
    bb = asset_data.get('bb') or {}
    adx = asset_data.get('adx') or {}
    volume = asset_data.get('volume_metrics') or {}
    div_data = asset_data.get('rsi_divergence') or {}

    # Get price
    price = asset_data.get('latest_price', 0)
    rt = asset_data.get('realtime') or {}
    if rt.get('price'):
        price = rt['price']

    # Divergence types
    daily_div = div_data.get('daily') or {}
    weekly_div = div_data.get('weekly') or {}

    # Volume ratio (current vs 20-day avg)
    vol_ratio = None
    if volume.get('avg_volume') and volume.get('latest_volume'):
        avg_v = volume['avg_volume']
        if avg_v > 0:
            vol_ratio = round(volume['latest_volume'] / avg_v, 2)

    # EMA Ribbon gap %
    ema_ribbon_gap = None
    if ema_ribbon.get('fast_ema') and ema_ribbon.get('slow_ema') and ema_ribbon['slow_ema'] != 0:
        ema_ribbon_gap = round(
            (ema_ribbon['fast_ema'] - ema_ribbon['slow_ema']) / ema_ribbon['slow_ema'] * 100, 2
        )

    # Trade levels
    levels = trade_levels or {}

    # Build flat row
    row = {
        'journal_id': journal_id,
        'user_id': user_id,
        'snapshot_type': snapshot_type,
        'asset': asset,
        'direction': direction,

        # Scores
        'score_trend': score_result['criteria'].get('trend', {}).get('score'),
        'score_momentum': score_result['criteria'].get('momentum', {}).get('score'),
        'score_rsi': score_result['criteria'].get('rsi', {}).get('score'),
        'score_structure': score_result['criteria'].get('structure', {}).get('score'),
        'score_level': score_result['criteria'].get('level', {}).get('score'),
        'score_macro': score_result['criteria'].get('macro', {}).get('score'),
        'score_total': score_result['total'],
        'verdict': score_result['verdict'],
        'vetoes': json.dumps(score_result['vetoes']) if score_result['vetoes'] else None,

        'price': price,

        # Consensio
        'consensio_1d_signal': _safe_int(consensio.get('1D', {}).get('signal')),
        'consensio_1d_positions': _safe_int(consensio.get('1D', {}).get('positions')),
        'consensio_3d_signal': _safe_int(consensio.get('3D', {}).get('signal')),
        'consensio_3d_positions': _safe_int(consensio.get('3D', {}).get('positions')),
        'consensio_1w_signal': _safe_int(consensio.get('1W', {}).get('signal')),
        'consensio_1w_positions': _safe_int(consensio.get('1W', {}).get('positions')),
        'consensio_total_positions': _safe_int(consensio.get('total', {}).get('positions')),

        # Momentum
        'ema_ribbon_trend': ema_ribbon.get('trend'),
        'ema_ribbon_gap_pct': ema_ribbon_gap,
        'macd_histogram': _safe_float(macd.get('histogram')),
        'macd_momentum': macd.get('momentum'),

        # RSI
        'weekly_rsi': _safe_float(asset_data.get('weekly_rsi')),
        'rsi_div_daily': daily_div.get('divergence_type'),
        'rsi_div_weekly': weekly_div.get('divergence_type'),

        # Structure
        'structure_trend': structure.get('structure_trend'),
        'last_high_type': structure.get('last_high_type'),
        'last_low_type': structure.get('last_low_type'),

        # Levels
        'cpr_pivot': _safe_float(cpr.get('pivot')),
        'cpr_tc': _safe_float(cpr.get('tc')),
        'cpr_bc': _safe_float(cpr.get('bc')),
        'bb_upper': _safe_float(bb.get('upper')),
        'bb_lower': _safe_float(bb.get('lower')),
        'bb_middle': _safe_float(bb.get('middle')),

        # Volume/ADX
        'adx_value': _safe_float(adx.get('adx') if isinstance(adx, dict) else None),
        'volume_ratio': vol_ratio,

        # Macro
        'regime': macro_data.get('regime'),
        'regime_score': macro_data.get('regime_score'),
        'fear_greed': macro_data.get('fear_greed'),
        'breadth_pct': _safe_float(macro_data.get('breadth', {}).get('pct')),

        # Asset-specific
        'funding_rate': _safe_float(asset_data.get('funding_rate')),

        # Trade levels
        'entry_price': _safe_float(levels.get('entry_price')),
        'stop_price': _safe_float(levels.get('stop_price')),
        'tp1_price': _safe_float(levels.get('tp1_price') or levels.get('target_price')),

        # Setup categorization
        'setup_type': (score_result.get('setup') or {}).get('type'),
        'setup_readiness': (score_result.get('setup') or {}).get('readiness'),
        'setup_confidence': _safe_float((score_result.get('setup') or {}).get('confidence')),
    }

    db.insert_score_snapshot(row)
    logger.info(f"Snapshot captured: {snapshot_type} {asset} {direction} "
                f"{score_result['total']}/12 {score_result['verdict']}")


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
