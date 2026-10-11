"""MCP tools: Context (3 tools)."""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError


def _price_lookup():
    """Return ``asset -> current price`` (or None), memoised per call."""
    from carrac.drawings.routes import _get_asset_price
    cache = {}

    def price_of(asset):
        if asset not in cache:
            cache[asset] = _get_asset_price(asset)
        return cache[asset]
    return price_of


def _level_dist(cp, level):
    from carrac.drawings.routes import _price_dist
    return _price_dist(cp, level)


def _zone_position(cp, lo, hi):
    if not cp or not lo or not hi:
        return ''
    if cp > hi:
        return f" [price ${cp:,.2f} is {(cp - hi) / hi * 100:.1f}% above zone]"
    if cp < lo:
        return f" [price ${cp:,.2f} is {(lo - cp) / lo * 100:.1f}% below zone]"
    return f" [price ${cp:,.2f} is INSIDE zone]"


def _position_pnl(cp, entry, direction):
    if not cp or not isinstance(entry, (int, float)) or not entry:
        return ''
    pnl = (cp - entry) / entry * 100
    if str(direction).lower() == 'short':
        pnl = -pnl
    return f" [P&L: {pnl:+.1f}%]"


@mcp_tool(
    'drawing_list',
    'Return the user\'s chart annotations (levels, trendlines, zones, scenarios, '
    'positions, fibonacci, etc.) as human-readable text grouped by asset. '
    'Each line has an [id:N] prefix so drawing IDs can be fed directly to '
    'drawing_delete or drawing_update in the same session. '
    'Filter to specific assets with the assets param (comma-separated or "all"). '
    'Set enrich=true (default) to include current-price distances alongside levels. '
    'Use before AI market analysis to give the model awareness of existing chart context.',
    {
        'type': 'object',
        'properties': {
            'assets': {
                'type': 'string',
                'description': 'Comma-separated symbols or "all" (default: all)',
            },
            'enrich': {
                'type': 'boolean',
                'description': 'Annotate with current prices and distances (default: true)',
            },
        },
    },
    min_tier='free',
)
def drawing_list(arguments):
    from carrac.content_db import get_content_db
    from carrac.mcp.tools import get_mcp_user_id

    assets = arguments.get('assets', 'all')
    enrich = arguments.get('enrich', True)

    db = get_content_db()
    if not db:
        raise ToolError('Drawing database not available')

    try:
        # Build the context text the same way the drawings context endpoint does

        # Scope to the caller's own drawings + system rows (user_id=0).
        # A user_id of -1 would disable ownership filtering and leak every
        # user's drawings to any per-user key.
        user_id = get_mcp_user_id()
        all_drawings = db.get_all_drawings_grouped(user_id=user_id)
        if not all_drawings:
            # Every sibling tool (play_list, alert_manage list, …)
            # returns a list key on the empty case — match that shape instead
            # of prose-only, and keep the prose as an extra field.
            return {'drawings': [], 'count': 0, 'text': 'No drawings found.'}

        # Filter by requested assets
        if assets and assets != 'all':
            filter_set = {s.strip().upper() for s in assets.split(',')}
            all_drawings = {k: v for k, v in all_drawings.items() if k.upper() in filter_set}

        # Build text output — id prefix on every line enables drawing_delete
        # Source structure is {asset: {timeframe: [drawing_dict, ...]}}
        lines = []
        total = 0
        price_of = _price_lookup() if enrich else (lambda _asset: None)
        for asset_key, by_tf in sorted(all_drawings.items()):
            if not isinstance(by_tf, dict):
                continue
            for _tf, drawings in by_tf.items():
                if not isinstance(drawings, list):
                    continue
                for d in drawings:
                    if not isinstance(d, dict):
                        continue
                    total += 1
                    did = d.get('id', '?')
                    dtype = d.get('type', d.get('drawing_type', 'level'))
                    label = d.get('label', '')
                    tf = d.get('timeframe', '')
                    prefix = f"[id:{did}] {asset_key} {tf}"
                    cp = price_of(asset_key)

                    if dtype in ('horizontal_line', 'level'):
                        price = d.get('price_level', d.get('price', d.get('y', 0)))
                        # Client-synced rows can carry prices only in metadata
                        # points — price_level None must not crash the listing.
                        price_txt = f"{price:,.2f}" if isinstance(price, (int, float)) else '?'
                        dist = _level_dist(cp, price) if isinstance(price, (int, float)) else ''
                        lines.append(f"{prefix}: Level at {price_txt}{dist}"
                                     + (f" (label: '{label}')" if label else ''))
                    elif dtype == 'trendline':
                        lines.append(f"{prefix}: Trendline"
                                     + (f" '{label}'" if label else ''))
                    elif dtype in ('rectangle', 'zone'):
                        y1 = d.get('price_level', d.get('y1', d.get('price', 0)))
                        y2 = d.get('price_level_upper', d.get('y2', 0))
                        if isinstance(y1, (int, float)) and isinstance(y2, (int, float)):
                            lo, hi = min(y1, y2), max(y1, y2)
                            zone_txt = f" {lo:,.2f}-{hi:,.2f}" + _zone_position(cp, lo, hi)
                        else:
                            zone_txt = ''  # client-synced rect: prices live in metadata points
                        lines.append(f"{prefix}: Zone{zone_txt}"
                                     + (f" (label: '{label}')" if label else ''))
                    elif dtype == 'fibonacci':
                        lines.append(f"{prefix}: Fibonacci"
                                     + (f" '{label}'" if label else ''))
                    elif dtype == 'position':
                        entry = d.get('entry_price', 0)
                        direction = d.get('direction', 'long')
                        lines.append(f"{prefix}: Position {direction} @ {entry:,.2f}"
                                     + _position_pnl(cp, entry, direction)
                                     + (f" '{label}'" if label else ''))
                    elif dtype == 'scenario':
                        lines.append(f"{prefix}: Scenario"
                                     + (f" '{label}'" if label else ''))
                    else:
                        lines.append(f"{prefix}: {dtype}"
                                     + (f" '{label}'" if label else ''))

        return {
            'text': '\n'.join(lines) if lines else 'No drawings found.',
            'count': total,
        }

    except Exception as e:
        raise ToolError(f'Failed to get drawings: {e}')


@mcp_tool(
    'drawing_delete',
    'Delete one or more chart drawings (levels, trendlines, zones, scenarios). '
    'Pass `drawing_id` for a single delete or `drawing_ids` for bulk. '
    'Use `dry_run` to preview without deleting. '
    'Hard delete — no undo (relies on the daily DB backup for recovery). '
    'Scenario parents cascade-delete their children.',
    {
        'type': 'object',
        'properties': {
            'drawing_id': {
                'type': 'integer',
                'description': 'Single drawing ID to delete',
            },
            'drawing_ids': {
                'type': 'array',
                'items': {'type': 'integer'},
                'description': 'List of drawing IDs to delete (max 50)',
            },
            'dry_run': {
                'type': 'boolean',
                'description': 'If true, return previews without deleting (default false)',
            },
        },
    },
    min_tier='free',  # owns own data — same as in-app delete button
)
def drawing_delete(arguments):
    from carrac.content_db import get_content_db
    from carrac.mcp.tools import get_mcp_user_id

    BULK_CAP = 50

    raw_id = arguments.get('drawing_id')
    raw_ids = arguments.get('drawing_ids') or []
    dry_run = bool(arguments.get('dry_run', False))

    ids = []
    if raw_id is not None:
        ids.append(int(raw_id))
    if raw_ids:
        if not isinstance(raw_ids, list):
            raise ToolError('drawing_ids must be an array of integers')
        try:
            ids.extend(int(i) for i in raw_ids)
        except (TypeError, ValueError):
            raise ToolError('drawing_ids must contain integers')
    # De-dupe while preserving order
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]

    if not ids:
        raise ToolError('Provide drawing_id or drawing_ids')
    if len(ids) > BULK_CAP:
        raise ToolError(f'Too many ids ({len(ids)}); max {BULK_CAP} per call')

    db = get_content_db()
    if not db:
        raise ToolError('Drawing database not available')

    user_id = get_mcp_user_id()

    # Build previews from the user's own rows (skips foreign IDs silently)
    previews = []
    skipped = []
    for did in ids:
        entry = db.get_drawing_by_id(did)
        if not entry or entry.get('user_id') != user_id:
            skipped.append(did)
            continue
        previews.append({
            'drawing_id': did,
            'asset': entry.get('asset'),
            'timeframe': entry.get('timeframe'),
            'drawing_type': entry.get('drawing_type'),
            'label': entry.get('label'),
            'price_level': entry.get('price_level'),
        })

    if dry_run:
        return {
            'dry_run': True,
            'would_delete': previews,
            'skipped': skipped,
            'count': len(previews),
        }

    deleted = db.delete_drawings_bulk([p['drawing_id'] for p in previews], user_id)
    return {
        'dry_run': False,
        'deleted_count': deleted,
        'deleted': previews,
        'skipped': skipped,
    }


@mcp_tool(
    'util_batch_query',
    'Fetch multiple data types for multiple assets in a single round-trip. '
    'Efficient for cross-asset analysis; avoids issuing separate tool calls per asset per block. '
    'Specify assets as a comma-separated string (max 10) and include as a comma-separated '
    'list of data blocks (default: "market,scanner"). '
    'Available blocks: market (OHLCV + indicators), scanner (signal scores), sentiment '
    '(funding/OI), alerts (active alerts), plays (active plays), positions (open positions), '
    'drawings (chart annotations). '
    'Requires member tier or above.',
    {
        'type': 'object',
        'properties': {
            'assets': {
                'type': 'string',
                'description': 'Comma-separated symbols (max 10). E.g. "BTC,ETH,SOL"',
            },
            'include': {
                'type': 'string',
                'description': 'Comma-separated data blocks (default: "market,scanner"). '
                               'Valid: market, scanner, sentiment, alerts, plays, positions, drawings',
            },
        },
        'required': ['assets'],
    },
    min_tier='member',
)
def util_batch_query(arguments):
    from carrac.composite.assemblers import assemble_batch, VALID_BLOCKS, MAX_BATCH_ASSETS
    from carrac.composite.formatters import compact_batch

    assets_str = arguments.get('assets', '')
    if not assets_str:
        raise ToolError('assets is required (comma-separated symbols)')

    assets = [s.strip().upper() for s in assets_str.split(',') if s.strip()]
    if not assets:
        raise ToolError('No valid symbols in assets')
    if len(assets) > MAX_BATCH_ASSETS:
        raise ToolError(f'Maximum {MAX_BATCH_ASSETS} assets per request')

    include_str = arguments.get('include', 'market,scanner')
    include_blocks = [b.strip() for b in include_str.split(',') if b.strip()]
    invalid = set(include_blocks) - VALID_BLOCKS
    if invalid:
        raise ToolError(f'Invalid blocks: {", ".join(sorted(invalid))}. '
                        f'Valid: {", ".join(sorted(VALID_BLOCKS))}')

    try:
        from carrac.mcp.tools import get_mcp_user_id
        caller_user_id = get_mcp_user_id()
    except ToolError:
        # Shared/admin key — no per-user binding; user-scoped blocks return empty.
        caller_user_id = None
    result = assemble_batch(assets, include_blocks, user_id=caller_user_id)
    return compact_batch(result)
