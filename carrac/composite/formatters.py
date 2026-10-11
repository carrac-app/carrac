"""Compact formatters for composite endpoint responses."""


# FTM was published with change_24h 362.06% (and change_pct_7d
# 1525.64%) — feed artefacts from the FTM→S token migration, republished as
# fact on the advertised channel. The bar is deliberately far above any real
# day: this catches a broken feed, not volatility. A genuine 60% move survives.
_ABSURD_24H_CHANGE_PCT = 100.0


def _plausible_change(pct):
    """Return ``pct``, or None when it is too large to be a real daily move."""
    if isinstance(pct, (int, float)) and abs(pct) > _ABSURD_24H_CHANGE_PCT:
        return None
    return pct


def compact_asset_detail(data):
    """Asset detail → ~1KB AI-optimized summary."""
    result = {
        'symbol': data.get('symbol'),
        'price': data.get('price'),
        'change_24h': _plausible_change(data.get('change_24h')),
    }

    # Must survive compaction, or the flag dies one layer short of
    # the surface that needed it.
    if data.get('data_stale'):
        result['data_stale'] = data['data_stale']

    # Technicals: keep key signals only
    tech = data.get('technicals')
    if tech:
        result['technicals'] = {
            'consensio': tech.get('consensio'),
            'aligned': tech.get('consensio_aligned'),
            'rsi_weekly': tech.get('rsi_weekly'),
            'ema_trend': tech.get('ema_ribbon_trend'),
        }

    # Scanner: keep as-is (already compact)
    if data.get('scanner'):
        result['scanner'] = data['scanner']

    # Sentiment: keep as-is (already compact)
    if data.get('sentiment'):
        result['sentiment'] = data['sentiment']

    # Plays: count + titles only
    plays = data.get('plays', [])
    result['plays_count'] = len(plays)
    if plays:
        result['plays'] = [{'id': p.get('id'), 'title': p.get('title'), 'status': p.get('status')} for p in plays[:3]]

    # Alerts: count only
    result['alerts_count'] = len(data.get('alerts', []))

    # Drawings: count only
    result['drawings_count'] = len(data.get('drawings', []))

    # Positions: summary only
    positions = data.get('positions', [])
    result['positions_count'] = len(positions)
    if positions:
        result['positions'] = [
            {'asset': p.get('asset'), 'direction': p.get('direction'),
             'pnl_pct': p.get('unrealized_pnl_pct'), 'risk': p.get('risk_status')}
            for p in positions[:3]
        ]

    return result


def compact_live_positions(data):
    """Live positions → compact summary."""
    positions = data.get('positions', [])
    return {
        'positions': [
            {
                'id': p.get('id'),
                'asset': p.get('asset'),
                'direction': p.get('direction'),
                'entry': p.get('entry_price'),
                'price': p.get('current_price'),
                'pnl_pct': p.get('unrealized_pnl_pct'),
                'rr': p.get('current_rr'),
                'risk': p.get('risk_status'),
                'scanner': p.get('scanner_verdict'),
                # Rolling MFE/MAE — null until the precompute hook
                # computes them; absent for non-crypto (no kline source).
                'mfe_r': p.get('mfe_r'),
                'mae_r': p.get('mae_r'),
                'mfe_computed_at': p.get('mfe_computed_at'),
            }
            for p in positions
        ],
        'heat': data.get('portfolio_heat', {}),
    }


def compact_batch(data):
    """Batch response → strip detail from per-asset blocks."""
    assets = data.get('assets', {})
    compacted = {}
    for sym, blocks in assets.items():
        entry = {}
        if 'market' in blocks:
            m = blocks['market']
            entry['price'] = m.get('price')
            entry['change'] = m.get('change_24h')
        if 'scanner' in blocks:
            entry['scanner'] = blocks['scanner']
        if 'alerts' in blocks:
            entry['alerts_count'] = len(blocks['alerts'])
        if 'plays' in blocks:
            entry['plays_count'] = len(blocks['plays'])
        if 'positions' in blocks:
            entry['positions_count'] = len(blocks['positions'])
        if 'drawings' in blocks:
            entry['drawings'] = blocks['drawings']

        # Applied once here, at the boundary, rather than inside
        # each `if ... in blocks` arm above. Every other key in this loop is
        # rebuilt field by field, which is exactly how the marker was lost:
        # the market arm copied price and change and nothing else. Read it
        # from whichever block carries it, so an include='scanner' call is
        # covered as well as include='market'.
        _stale = ((blocks.get('market') or {}).get('data_stale')
                  or (blocks.get('scanner') or {}).get('data_stale'))
        if _stale:
            entry['data_stale'] = _stale

        compacted[sym] = entry

    result = {'assets': compacted}
    if data.get('global'):
        result['global'] = data['global']
    return result
